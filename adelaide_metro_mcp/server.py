"""MCP tool definitions.

This module is kept thin: each tool validates its input, calls a query in
db.py, and formats a short text answer. Data access lives in db.py so it can
be tested without an MCP client.

Note: with the stdio transport, stdout carries the MCP protocol messages, so
nothing in this server may print() to stdout. Use logging (stderr) instead.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from typing import Annotated

from pydantic import Field

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from adelaide_metro_mcp import db

ATTRIBUTION = "Adelaide Metro – Department for Infrastructure and Transport, South Australia"

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

mcp = MCPServer(
    name="adelaide-metro",
    instructions=(
        "Answers questions about Adelaide Metro buses, trains and trams using official "
        "GTFS timetable data. To get departures, first call find_stops to get a stop_id. "
        "For 'how do I get from A to B', find stops near both places and call next_departures "
        "with to_stop_id to see which services go there directly and when they arrive. "
        "If no stop near B is known, or no trip goes there directly, call next_departures "
        "without to_stop_id, then trip_stops with a trip id and from_stop_id to see where that "
        "service goes, and pick the stop nearest B. "
        f"Data source: {ATTRIBUTION}."
    ),
)

Limit = Annotated[int, Field(ge=1, le=db.MAX_LIMIT, description=f"Maximum results (1-{db.MAX_LIMIT}).")]


def _run(query_fn):
    """Open the database, run a query, and turn expected failures into ToolErrors.

    ToolError messages are shown to the client as-is. Any other exception is
    treated by the SDK as a crash and its message is hidden, which is what we
    want for genuine bugs.
    """
    try:
        with closing(db.connect()) as conn:
            return query_fn(conn)
    except (ValueError, db.NotFoundError, db.DatabaseNotReadyError) as exc:
        raise ToolError(str(exc)) from exc


def _format_stop(s: db.Stop) -> str:
    code = f"stop {s.stop_code}, " if s.stop_code else ""
    desc = f" - {s.stop_desc}" if s.stop_desc else ""
    return f"{s.stop_name} ({code}id {s.stop_id}){desc}"


@mcp.tool(annotations=READ_ONLY)
def find_stops(
    query: Annotated[str, Field(description="Stop name, street, suburb or stop number, e.g. 'Glenelg' or 'King William D2'.")],
    limit: Limit = 10,
) -> str:
    """Search Adelaide Metro stops by name, street, suburb or the stop number shown on the sign.

    Returns each stop's name, stop number, id and address. Use the id with
    next_departures or routes_at_stop.
    """
    stops = _run(lambda conn: db.find_stops(conn, query, limit))
    if not stops:
        return f"No stops found for {query.strip()!r}. Try a shorter name or a street name."
    lines = [f"Found {len(stops)} stop(s) for {query.strip()!r}:"]
    lines += [f"- {_format_stop(s)}" for s in stops]
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def routes_at_stop(
    stop_id: Annotated[str, Field(description="Stop id from find_stops.")],
) -> str:
    """List the bus, train and tram routes that stop at a given stop."""
    stop, routes = _run(lambda conn: db.routes_at_stop(conn, stop_id))
    if not routes:
        return f"No scheduled routes found at {_format_stop(stop)}."
    lines = [f"Routes at {_format_stop(stop)}:"]
    lines += [f"- {r.short_name} ({r.mode}): {r.long_name}" for r in routes]
    return "\n".join(lines)


def _now() -> datetime:
    """Current time in Adelaide. A separate function so tests can replace it."""
    return datetime.now(db.ADELAIDE_TZ)


def _day_suffix(when: datetime, now: datetime) -> str:
    return "" if when.date() == now.date() else " tomorrow"


def _format_departure(d: db.Departure, now: datetime) -> str:
    minutes = int((d.departs_at - now).total_seconds() // 60)
    if minutes <= 0:
        when = "now"
    elif minutes < 60:
        when = f"in {minutes} min"
    else:
        hours, mins = divmod(minutes, 60)
        when = f"in {hours} h" if mins == 0 else f"in {hours} h {mins} min"
    destination = d.headsign or d.route_long_name
    line = f"- {d.departs_at:%H:%M}{_day_suffix(d.departs_at, now)}  {d.mode} {d.route_short_name} to {destination} ({when})"
    if d.arrives_at is not None:
        ride = int((d.arrives_at - d.departs_at).total_seconds() // 60)
        line += f", arrives {d.arrives_at:%H:%M}{_day_suffix(d.arrives_at, now)} ({ride} min ride)"
    return f"{line}, trip {d.trip_id}"


@mcp.tool(annotations=READ_ONLY)
def next_departures(
    stop_id: Annotated[str, Field(description="Stop id from find_stops.")],
    limit: Limit = 10,
    minutes_ahead: Annotated[
        int,
        Field(ge=1, le=db.MAX_MINUTES_AHEAD, description="How far ahead to look, in minutes (1-1440). Default 120."),
    ] = db.DEFAULT_MINUTES_AHEAD,
    route: Annotated[
        str | None, Field(description="Only this route, e.g. '100' or 'GLNELG' (Glenelg tram). Omit for all routes.")
    ] = None,
    to_stop_id: Annotated[
        str | None,
        Field(
            description="Optional destination stop id from find_stops. Only trips that stop there later "
            "are listed, with their arrival time. Direct trips only (no transfers)."
        ),
    ] = None,
) -> str:
    """Upcoming scheduled departures from a stop, in Adelaide local time.

    Based on the published timetable only: it does not include live delays or
    cancellations. Covers the next `minutes_ahead` minutes (default 2 hours).
    To answer "how do I get from A to B", pass the destination as to_stop_id:
    the result shows which buses go there directly and when each one arrives.
    """
    now = _now()
    board = _run(lambda conn: db.next_departures(conn, stop_id, now, limit, minutes_ahead, route, to_stop_id))

    kind = "direct trips" if board.to_stop else "departures"
    route_text = f" for route {route.strip()}" if route and route.strip() else ""
    to_text = f" to {_format_stop(board.to_stop)}" if board.to_stop else ""
    trip_text = f"{kind}{route_text} from {_format_stop(board.stop)}{to_text}"
    if not board.departures:
        window = f"{minutes_ahead} minutes" if minutes_ahead < 120 else f"{minutes_ahead / 60:g} hours"
        return f"No scheduled {trip_text} in the next {window}."
    lines = [
        f"Scheduled {trip_text}",
        f"As of {now:%a %d %b %H:%M} (timetable only, no live delays):",
    ]
    lines += [_format_departure(d, now) for d in board.departures]
    return "\n".join(lines)


def _format_trip_time(secs: int | None, first_secs: int | None) -> str:
    """HH:MM clock time; '(next day)' when the trip has crossed midnight since the first listed stop.

    GTFS times count from the trip's service day and can pass 24:00
    (25:10 = 01:10 next morning). Comparing with the first listed stop means a
    trip that starts after midnight is not marked, but one that crosses it is.
    """
    if secs is None:
        return "--:--"
    hours, rem = divmod(secs, 3600)
    clock = f"{hours % 24:02d}:{rem // 60:02d}"
    if first_secs is not None and secs // 86400 > first_secs // 86400:
        clock += " (next day)"
    return clock


@mcp.tool(annotations=READ_ONLY)
def trip_stops(
    trip_id: Annotated[str, Field(description="Trip id from a next_departures result (the 'trip ...' at the end of a line).")],
    from_stop_id: Annotated[
        str | None,
        Field(description="Optional stop id where the rider boards. Only the stops after it are listed."),
    ] = None,
) -> str:
    """Every stop a trip calls at, in order, with scheduled times (Adelaide local time).

    Use it to find where a bus, train or tram goes and which stop to get off at
    when you don't know the destination stop id. Timetable only, no live delays.
    """
    trip = _run(lambda conn: db.trip_stops(conn, trip_id, from_stop_id))
    destination = trip.headsign or trip.route_long_name
    lines = [f"{trip.mode} {trip.route_short_name} to {destination} (trip {trip.trip_id})"]
    if trip.from_stop is not None:
        lines.append(f"Stops after {_format_stop(trip.from_stop)}, timetable times:")
    else:
        lines.append("All stops, timetable times:")
    if not trip.stops:
        lines.append("- none: this is the trip's last stop")
        return "\n".join(lines)

    first_secs = trip.stops[0].time_secs
    for i, ts in enumerate(trip.stops):
        notes = []
        if not ts.can_alight and ts.can_board:
            notes.append("pick up only")
        elif not ts.can_board and ts.can_alight:
            notes.append("set down only")
        elif not ts.can_board and not ts.can_alight:
            notes.append("does not stop for passengers")
        if i == len(trip.stops) - 1:
            notes.append("last stop")
        note = f" ({', '.join(notes)})" if notes else ""
        lines.append(f"- {_format_trip_time(ts.time_secs, first_secs)}  {_format_stop(ts.stop)}{note}")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def timetable_info() -> str:
    """Which Adelaide Metro timetable version is loaded and the dates it covers."""
    info = _run(db.timetable_info)
    today = _now().date()
    lines = [
        f"Timetable version {info.feed_version or 'unknown'}",
        f"Valid from {info.start_date or 'unknown'} to {info.end_date or 'unknown'}",
        f"Downloaded {info.loaded_at_utc or 'unknown'} (UTC)",
        f"{info.stop_count} stops, {info.route_count} routes, {info.trip_count} trips",
    ]
    if info.end_date and today > info.end_date:
        lines.append("Warning: this timetable has expired. Run: python -m adelaide_metro_mcp.gtfs_loader")
    elif info.start_date and today < info.start_date:
        lines.append("Note: this timetable has not started yet; departures for today may be missing.")
    lines.append(f"Data source: {ATTRIBUTION}")
    return "\n".join(lines)


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run()  # transport defaults to "stdio"


if __name__ == "__main__":
    main()
