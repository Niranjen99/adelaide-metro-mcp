"""Read-only queries against the GTFS SQLite database.

Kept separate from the MCP layer so the logic is testable with plain pytest.
Every function takes an open sqlite3 connection and returns plain data;
formatting for humans happens in server.py.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from adelaide_metro_mcp.gtfs_loader import DB_FILENAME, default_data_dir

# All GTFS times are local Adelaide time (agency_timezone in agency.txt).
# ZoneInfo handles daylight saving (ACST +9:30 / ACDT +10:30).
ADELAIDE_TZ = ZoneInfo("Australia/Adelaide")

# calendar.txt weekday columns, indexed by date.weekday() (Monday = 0).
_WEEKDAY_COLUMNS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# GTFS route_type values present in the Adelaide Metro feed.
# 0-4 are core GTFS; 701 and 712 are Google's extended bus types.
ROUTE_TYPE_NAMES = {
    0: "Tram",
    2: "Train",
    3: "Bus",
    4: "Ferry",
    701: "Regional bus",
    712: "School bus",
}

MAX_QUERY_LENGTH = 100
MAX_LIMIT = 20
DEFAULT_MINUTES_AHEAD = 120
MAX_MINUTES_AHEAD = 24 * 60
_STOP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
_ROUTE_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


class DatabaseNotReadyError(RuntimeError):
    """The timetable has not been downloaded yet."""


class NotFoundError(LookupError):
    """A requested stop or route does not exist."""


@dataclass(frozen=True)
class Stop:
    stop_id: str
    stop_code: str
    stop_name: str
    stop_desc: str


@dataclass(frozen=True)
class RouteSummary:
    route_id: str
    short_name: str
    long_name: str
    mode: str


@dataclass(frozen=True)
class Departure:
    departs_at: datetime  # timezone-aware, Adelaide time
    route_short_name: str
    route_long_name: str
    mode: str
    headsign: str
    trip_id: str


@dataclass(frozen=True)
class TimetableInfo:
    feed_version: str
    start_date: date | None
    end_date: date | None
    loaded_at_utc: str
    stop_count: int
    route_count: int
    trip_count: int


# --- connection ---------------------------------------------------------------

def db_path() -> Path:
    return default_data_dir() / DB_FILENAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database read-only. The server never writes to it."""
    path = Path(path) if path else db_path()
    if not path.exists():
        raise DatabaseNotReadyError(
            "The timetable has not been downloaded yet. "
            "Run: python -m adelaide_metro_mcp.gtfs_loader"
        )
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


# --- input validation ----------------------------------------------------------

def validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be a whole number")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be text")
    query = " ".join(query.split())  # trim and collapse whitespace
    if len(query) < 2:
        raise ValueError("query must be at least 2 characters")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")
    return query


def validate_stop_id(stop_id: str) -> str:
    if not isinstance(stop_id, str):
        raise ValueError("stop_id must be text")
    stop_id = stop_id.strip()
    if not _STOP_ID_RE.match(stop_id):
        raise ValueError("stop_id must be 1-20 letters, digits, '-' or '_' (use the id from find_stops)")
    return stop_id


def validate_minutes_ahead(minutes: int) -> int:
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise ValueError("minutes_ahead must be a whole number")
    if not 1 <= minutes <= MAX_MINUTES_AHEAD:
        raise ValueError(f"minutes_ahead must be between 1 and {MAX_MINUTES_AHEAD} (24 hours)")
    return minutes


def validate_route(route: str | None) -> str | None:
    """Route filter: the number on the bus/tram (e.g. '100', 'GLNELG'). Empty means no filter."""
    if route is None:
        return None
    if not isinstance(route, str):
        raise ValueError("route must be text")
    route = route.strip()
    if not route:
        return None
    if not _ROUTE_RE.match(route):
        raise ValueError("route must be 1-20 letters, digits, '-' or '_', e.g. '100' or 'GLNELG'")
    return route


def _escape_like(term: str) -> str:
    """Make % and _ in user input match literally in a LIKE pattern."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- queries ---------------------------------------------------------------------

def find_stops(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[Stop]:
    """Find boarding stops by name, street/description, or stop number.

    Every word in the query must appear in the stop name or description,
    in any order, case-insensitive. So "king william d2" finds
    "Stop D2 King William St - East side". A query that exactly matches a
    stop number (the code printed on the stop sign) is listed first.
    Parent stations (location_type 1) are excluded: they have no departures.
    """
    query = validate_query(query)
    limit = validate_limit(limit)

    words = query.split()
    conditions = []
    params: list[object] = []
    for w in words:
        conditions.append(
            "(stop_name LIKE ? ESCAPE '\\' OR IFNULL(stop_desc, '') LIKE ? ESCAPE '\\')"
        )
        pattern = f"%{_escape_like(w)}%"
        params += [pattern, pattern]

    sql = f"""
        SELECT stop_id, IFNULL(stop_code, ''), stop_name, IFNULL(stop_desc, '')
        FROM stops
        WHERE IFNULL(location_type, 0) = 0
          AND (stop_code = ? OR stop_id = ? OR ({' AND '.join(conditions)}))
        ORDER BY
          CASE WHEN stop_code = ? OR stop_id = ? THEN 0
               WHEN stop_name LIKE ? ESCAPE '\\' THEN 1
               ELSE 2 END,
          stop_name, stop_code
        LIMIT ?
    """
    starts_with = f"{_escape_like(query)}%"
    all_params = [query, query, *params, query, query, starts_with, limit]
    return [Stop(*row) for row in conn.execute(sql, all_params)]


def get_stop(conn: sqlite3.Connection, stop_id: str) -> Stop:
    """Return one boarding stop, or raise NotFoundError."""
    stop_id = validate_stop_id(stop_id)
    row = conn.execute(
        "SELECT stop_id, IFNULL(stop_code, ''), stop_name, IFNULL(stop_desc, ''), "
        "IFNULL(location_type, 0) FROM stops WHERE stop_id = ?",
        (stop_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"No stop with id {stop_id!r}. Use find_stops to look up the id.")
    if row[4] == 1:
        raise NotFoundError(
            f"{row[2]!r} (id {stop_id}) is a station grouping, not a boarding stop. "
            "Use find_stops to pick a specific platform or stop."
        )
    return Stop(*row[:4])


def routes_at_stop(conn: sqlite3.Connection, stop_id: str) -> tuple[Stop, list[RouteSummary]]:
    """Return the stop and every route with at least one scheduled trip calling there."""
    stop = get_stop(conn, stop_id)
    rows = conn.execute(
        """
        SELECT DISTINCT r.route_id, IFNULL(r.route_short_name, ''),
               IFNULL(r.route_long_name, ''), r.route_type
        FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE st.stop_id = ?
        """,
        (stop.stop_id,),
    ).fetchall()
    routes = [
        RouteSummary(rid, short, long_, ROUTE_TYPE_NAMES.get(rtype, f"Type {rtype}"))
        for rid, short, long_, rtype in rows
    ]
    routes.sort(key=_route_sort_key)
    return stop, routes


def _route_sort_key(r: RouteSummary):
    """Natural order: 100, 101, 150P, 200 ... rather than 100, 1000, 101."""
    m = re.match(r"(\d+)(.*)", r.short_name)
    return (0, int(m.group(1)), m.group(2)) if m else (1, 0, r.short_name)


# --- timetable: which trips run when ------------------------------------------------

def _gtfs_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _parse_gtfs_date(value: str | None) -> date | None:
    try:
        return datetime.strptime(value or "", "%Y%m%d").date()
    except ValueError:
        return None


def active_services(conn: sqlite3.Connection, service_day: date) -> set[str]:
    """Return the service_ids that run on a given service day.

    A service runs if calendar.txt says so (weekday flag set and the date is in
    its start/end range), plus calendar_dates.txt exceptions for that date:
    exception_type 1 adds the service, 2 removes it (e.g. a public holiday).
    """
    day_col = _WEEKDAY_COLUMNS[service_day.weekday()]  # from a fixed tuple, never user input
    d = _gtfs_date(service_day)
    rows = conn.execute(
        f"""
        SELECT service_id FROM calendar
        WHERE {day_col} = 1 AND start_date <= ? AND end_date >= ?
        UNION
        SELECT service_id FROM calendar_dates WHERE date = ? AND exception_type = 1
        EXCEPT
        SELECT service_id FROM calendar_dates WHERE date = ? AND exception_type = 2
        """,
        (d, d, d, d),
    )
    return {r[0] for r in rows}


def service_day_start(service_day: date) -> datetime:
    """The instant that GTFS time 00:00:00 refers to on a service day.

    GTFS defines times as "seconds since noon minus 12 hours", not since
    midnight. On normal days that is midnight; on the two daylight-saving
    change days it is one hour off midnight, so a trip at 08:00 still means
    08:00 on the clock. Computed in UTC to avoid wall-clock arithmetic.
    """
    noon = datetime.combine(service_day, time(12), tzinfo=ADELAIDE_TZ)
    return noon.astimezone(timezone.utc) - timedelta(hours=12)


def next_departures(
    conn: sqlite3.Connection,
    stop_id: str,
    now: datetime,
    limit: int = 10,
    minutes_ahead: int = DEFAULT_MINUTES_AHEAD,
    route: str | None = None,
) -> tuple[Stop, list[Departure]]:
    """Scheduled departures from a stop between `now` and `now + minutes_ahead`.

    Timetable only (no live delays). A trip belongs to the service day it
    started on, and its times can exceed 24:00 (e.g. 24:50 = 00:50 next
    morning). So for each of yesterday, today and tomorrow we convert the time
    window into that day's "seconds since service-day start" and query it.
    Yesterday catches late-night trips after midnight; tomorrow is only needed
    when the window reaches past midnight.

    Skipped: stops where you cannot board (pickup_type 1) and the last stop
    of each trip (the vehicle terminates there, so it is not a departure).
    """
    stop = get_stop(conn, stop_id)
    limit = validate_limit(limit)
    minutes_ahead = validate_minutes_ahead(minutes_ahead)
    route = validate_route(route)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise TypeError("now must be a timezone-aware datetime")  # programming error, not user input

    if route is not None:
        _check_route_serves_stop(conn, route, stop)

    window_end = now + timedelta(minutes=minutes_ahead)
    today = now.astimezone(ADELAIDE_TZ).date()
    departures: list[Departure] = []

    for service_day in (today - timedelta(days=1), today, today + timedelta(days=1)):
        services = active_services(conn, service_day)
        if not services:
            continue
        base = service_day_start(service_day)
        lo = int((now - base).total_seconds())
        hi = int((window_end - base).total_seconds())
        if hi < 0:
            continue  # the window ends before this service day starts

        placeholders = ",".join("?" * len(services))
        route_sql = ""
        params: list[object] = [stop.stop_id, lo, hi, *sorted(services)]
        if route is not None:
            route_sql = "AND (r.route_short_name = ? COLLATE NOCASE OR r.route_id = ? COLLATE NOCASE)"
            params += [route, route]
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT st.departure_secs, IFNULL(r.route_short_name, ''), IFNULL(r.route_long_name, ''),
                   r.route_type, IFNULL(t.trip_headsign, ''), t.trip_id
            FROM stop_times st
            JOIN trips t ON t.trip_id = st.trip_id
            JOIN routes r ON r.route_id = t.route_id
            WHERE st.stop_id = ?
              AND st.departure_secs BETWEEN ? AND ?
              AND IFNULL(st.pickup_type, 0) != 1
              AND t.service_id IN ({placeholders})
              {route_sql}
              AND EXISTS (SELECT 1 FROM stop_times nxt
                          WHERE nxt.trip_id = st.trip_id AND nxt.stop_sequence > st.stop_sequence)
            ORDER BY st.departure_secs
            LIMIT ?
            """,
            params,
        ).fetchall()

        for secs, short, long_, rtype, headsign, trip_id in rows:
            departs_at = (base + timedelta(seconds=secs)).astimezone(ADELAIDE_TZ)
            departures.append(
                Departure(departs_at, short, long_, ROUTE_TYPE_NAMES.get(rtype, f"Type {rtype}"), headsign, trip_id)
            )

    departures.sort(key=lambda d: (d.departs_at, d.route_short_name, d.trip_id))
    return stop, departures[:limit]


def _check_route_serves_stop(conn: sqlite3.Connection, route: str, stop: Stop) -> None:
    """Tell 'no such route' and 'route never stops here' apart from 'none soon'.

    Without this, asking for a tram at a bus stop would answer "no departures
    in the next 2 hours", which wrongly suggests waiting longer would help.
    """
    match = "(r.route_short_name = ? COLLATE NOCASE OR r.route_id = ? COLLATE NOCASE)"
    if conn.execute(f"SELECT 1 FROM routes r WHERE {match}", (route, route)).fetchone() is None:
        raise NotFoundError(f"No route {route!r}. Use routes_at_stop to see the routes at this stop.")
    serves = conn.execute(
        f"""
        SELECT 1 FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE st.stop_id = ? AND {match}
        LIMIT 1
        """,
        (stop.stop_id, route, route),
    ).fetchone()
    if serves is None:
        raise NotFoundError(
            f"Route {route} does not stop at {stop.stop_name} (id {stop.stop_id}). "
            "Use routes_at_stop to see the routes that do."
        )


def timetable_info(conn: sqlite3.Connection) -> TimetableInfo:
    """Which timetable version is loaded, the dates it covers, and its size."""
    meta = dict(conn.execute("SELECT key, value FROM metadata"))
    stops, routes, trips = conn.execute(
        "SELECT (SELECT COUNT(*) FROM stops WHERE IFNULL(location_type, 0) = 0), "
        "(SELECT COUNT(*) FROM routes), (SELECT COUNT(*) FROM trips)"
    ).fetchone()
    return TimetableInfo(
        feed_version=meta.get("feed_version", ""),
        start_date=_parse_gtfs_date(meta.get("feed_start_date")),
        end_date=_parse_gtfs_date(meta.get("feed_end_date")),
        loaded_at_utc=meta.get("loaded_at_utc", ""),
        stop_count=stops,
        route_count=routes,
        trip_count=trips,
    )
