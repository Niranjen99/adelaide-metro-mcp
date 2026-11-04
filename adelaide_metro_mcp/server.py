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
        "Each stop shows 'towards' (where its trips go): use it to pick the side of the road "
        "going the right way. If the right side isn't listed, call find_stops with near_stop_id "
        "set to a stop you found; addresses on the two sides can name different suburbs. "
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
    query: Annotated[
        str | None,
        Field(description="Stop name, street, suburb or stop number, e.g. 'Glenelg' or 'King William D2'."),
    ] = None,
    limit: Limit = 10,
    near_stop_id: Annotated[
        str | None,
        Field(
            description="Optional stop id: only stops within radius_m of it, nearest first. Use it to find "
            "the stop on the other side of the road, or other stops nearby. Distances are straight-line "
            "between stops, not walking routes."
        ),
    ] = None,
    radius_m: Annotated[
        int,
        Field(ge=db.MIN_RADIUS_M, le=db.MAX_RADIUS_M, description="Search radius for near_stop_id, in metres. Default 400."),
    ] = db.DEFAULT_RADIUS_M,
) -> str:
    """Search Adelaide Metro stops by name, street, suburb or stop number, or near another stop.

    Give a query, a near_stop_id, or both. Returns each stop's name, stop
    number, id, address and 'towards' (where most departing trips go, which
    tells the two sides of a road apart). Use the id with next_departures.
    """
    stops = _run(lambda conn: db.find_stops(conn, query, limit, near_stop_id, radius_m))
    what = ""
    if query is not None:
        what += f" matching {query.strip()!r}"
    if near_stop_id is not None:
        what += f" within {radius_m} m of stop id {near_stop_id.strip()}"
    if not stops:
        return f"No stops found{what}. Try a shorter name, a street name or a bigger radius."
    lines = [f"Found {len(stops)} stop(s){what}:"]
    lines += [f"- {_format_found_stop(s)}" for s in stops]
    return "\n".join(lines)


def _format_found_stop(s: db.Stop) -> str:
    line = _format_stop(s)
    if s.distance_m is not None:
        line += f", {s.distance_m} m away"
    line += f"; towards {', '.join(s.towards)}" if s.towards else "; no departures (trips end here)"
    return line


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
    max_stops: Annotated[
        int,
        Field(ge=1, le=db.MAX_MAX_STOPS, description="Maximum stops to list (1-200). Default 25; the last stop is always named."),
    ] = db.DEFAULT_MAX_STOPS,
) -> str:
    """Every stop a trip calls at, in order, with scheduled times (Adelaide local time).

    Use it to find where a bus, train or tram goes and which stop to get off at
    when you don't know the destination stop id. Timetable only, no live delays.
    """
    trip = _run(lambda conn: db.trip_stops(conn, trip_id, from_stop_id, max_stops))
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
        if i == len(trip.stops) - 1 and not trip.more_stops:
            notes.append("last stop")
        note = f" ({', '.join(notes)})" if notes else ""
        lines.append(f"- {_format_trip_time(ts.time_secs, first_secs)}  {_format_stop(ts.stop)}{note}")
    if trip.more_stops:
        last = trip.last_stop
        lines.append(
            f"... {trip.more_stops} more stop(s), ending at {last.stop.stop_name} (id {last.stop.stop_id}) "
            f"at {_format_trip_time(last.time_secs, first_secs)}. Ask with a bigger max_stops to see them."
        )
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
