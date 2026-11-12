"""Read-only queries against the GTFS SQLite database.

Kept separate from the MCP layer so the logic is testable with plain pytest.
Every function takes an open sqlite3 connection and returns plain data;
formatting for humans happens in server.py.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from adelaide_metro_mcp.gtfs_loader import DB_FILENAME, SCHEMA_VERSION, default_data_dir

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
DEFAULT_RADIUS_M = 400  # about a 5-minute walk
MIN_RADIUS_M, MAX_RADIUS_M = 50, 2000
DEFAULT_MAX_STOPS = 25
MAX_MAX_STOPS = 200  # the longest real trip has 177 stops
TOWARDS_SHOWN = 2  # headsigns listed per stop in find_stops
_EARTH_RADIUS_M = 6_371_000
_METRES_PER_DEGREE_LAT = 111_320
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")  # stop ids and route names in the real feed fit this

# The stops columns that make up a Stop, in field order.
_STOP_COLUMNS = "stop_id, IFNULL(stop_code, ''), stop_name, IFNULL(stop_desc, '')"

# A route can be given by its short name ("100", "GLNELG") or its route_id.
_ROUTE_MATCH_SQL = "(r.route_short_name = ? COLLATE NOCASE OR r.route_id = ? COLLATE NOCASE)"


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
    towards: tuple[str, ...] = ()  # main headsigns of departing trips (find_stops only)
    distance_m: int | None = None  # from the near_stop_id stop (find_stops only)


@dataclass(frozen=True)
class RouteSummary:
    route_id: str  # not shown to users; service alerts (Phase 2) identify routes by route_id
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
    arrives_at: datetime | None = None  # at the destination, when to_stop_id is given


@dataclass(frozen=True)
class DepartureBoard:
    stop: Stop
    to_stop: Stop | None  # set when to_stop_id was given
    departures: list[Departure]


@dataclass(frozen=True)
class TripStop:
    stop: Stop
    stop_sequence: int
    time_secs: int | None  # arrival (or departure) time, seconds since the trip's service-day start
    can_board: bool  # False when pickup_type is 1 (set down only)
    can_alight: bool  # False when drop_off_type is 1 (pick up only)


@dataclass(frozen=True)
class TripStops:
    trip_id: str
    route_short_name: str
    route_long_name: str
    mode: str
    headsign: str
    from_stop: Stop | None  # set when from_stop_id was given
    stops: list[TripStop]  # in travel order, at most max_stops
    more_stops: int = 0  # stops left out after the max_stops cut
    last_stop: TripStop | None = None  # the trip's final stop, when it was cut off


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
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        row = None
    if row is None or row[0] != SCHEMA_VERSION:
        conn.close()
        raise DatabaseNotReadyError(
            "The timetable database was built by an older version of this server. "
            "Rebuild it with: python -m adelaide_metro_mcp.gtfs_loader"
        )
    return conn


# --- input validation ----------------------------------------------------------

def _validate_int(value: int, name: str, low: int, high: int, hint: str = "") -> int:
    # bool is a subclass of int in Python, so True would otherwise pass as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}{hint}")
    return value


def validate_limit(limit: int) -> int:
    return _validate_int(limit, "limit", 1, MAX_LIMIT)


def validate_radius(radius_m: int) -> int:
    return _validate_int(radius_m, "radius_m", MIN_RADIUS_M, MAX_RADIUS_M, " metres")


def validate_max_stops(max_stops: int) -> int:
    return _validate_int(max_stops, "max_stops", 1, MAX_MAX_STOPS)


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
    if not _ID_RE.match(stop_id):
        raise ValueError("stop_id must be 1-20 letters, digits, '-' or '_' (use the id from find_stops)")
    return stop_id


def validate_trip_id(trip_id: str) -> str:
    if not isinstance(trip_id, str):
        raise ValueError("trip_id must be text")
    trip_id = trip_id.strip()
    if not _ID_RE.match(trip_id):
        raise ValueError("trip_id must be 1-20 letters, digits, '-' or '_' (use the trip id from next_departures)")
    return trip_id


def validate_minutes_ahead(minutes: int) -> int:
    return _validate_int(minutes, "minutes_ahead", 1, MAX_MINUTES_AHEAD, " (24 hours)")


def validate_route(route: str | None) -> str | None:
    """Route filter: the number on the bus/tram (e.g. '100', 'GLNELG'). Empty means no filter."""
    if route is None:
        return None
    if not isinstance(route, str):
        raise ValueError("route must be text")
    route = route.strip()
    if not route:
        return None
    if not _ID_RE.match(route):
        raise ValueError("route must be 1-20 letters, digits, '-' or '_', e.g. '100' or 'GLNELG'")
    return route


def _escape_like(term: str) -> str:
    """Make % and _ in user input match literally in a LIKE pattern."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _mode_name(route_type: int) -> str:
    return ROUTE_TYPE_NAMES.get(route_type, f"Type {route_type}")


# --- queries ---------------------------------------------------------------------

def find_stops(
    conn: sqlite3.Connection,
    query: str | None = None,
    limit: int = 10,
    near_stop_id: str | None = None,
    radius_m: int = DEFAULT_RADIUS_M,
) -> list[Stop]:
    """Find boarding stops by text, by distance from another stop, or both.

    Text: every word in the query must appear in the stop name or description,
    in any order, case-insensitive. So "king william d2" finds
    "Stop D2 King William St - East side". A query that exactly matches a
    stop number (the code printed on the stop sign) is listed first.

    near_stop_id: only stops within radius_m metres of that stop, nearest
    first (the stop itself is included, at 0 m). This finds the stop on the
    other side of the road even when its address names a different suburb.

    Each stop comes with `towards`: where most trips departing from it are
    headed, so the right direction can be picked without more calls.
    Parent stations (location_type 1) are excluded: they have no departures.
    """
    if query is None and near_stop_id is None:
        raise ValueError("give a query, a near_stop_id, or both")
    limit = validate_limit(limit)
    radius_m = validate_radius(radius_m)
    query = validate_query(query) if query is not None else None

    where = ["IFNULL(location_type, 0) = 0"]
    params: list[object] = []
    if query is not None:
        words = []
        for w in query.split():
            words.append("(stop_name LIKE ? ESCAPE '\\' OR IFNULL(stop_desc, '') LIKE ? ESCAPE '\\')")
            pattern = f"%{_escape_like(w)}%"
            params += [pattern, pattern]
        where.append(f"(stop_code = ? OR stop_id = ? OR ({' AND '.join(words)}))")
        params = [query, query, *params]

    if near_stop_id is None:
        starts_with = f"{_escape_like(query)}%"
        rows = conn.execute(
            f"""
            SELECT {_STOP_COLUMNS} FROM stops
            WHERE {' AND '.join(where)}
            ORDER BY
              CASE WHEN stop_code = ? OR stop_id = ? THEN 0
                   WHEN stop_name LIKE ? ESCAPE '\\' THEN 1
                   ELSE 2 END,
              stop_name, stop_code
            LIMIT ?
            """,
            [*params, query, query, starts_with, limit],
        ).fetchall()
        stops = [Stop(*row) for row in rows]
    else:
        stops = _stops_near(conn, near_stop_id, radius_m, where, params)[:limit]

    return _with_towards(conn, stops)


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (haversine) distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _stops_near(
    conn: sqlite3.Connection, near_stop_id: str, radius_m: int, where: list[str], params: list[object]
) -> list[Stop]:
    """Stops within radius_m of a stop (plus any text conditions), nearest first.

    SQL first narrows the search to a lat/lon box around the stop (cheap
    comparisons), then exact distances are computed in Python for the few
    stops inside the box, and the box corners are dropped.
    """
    centre = get_stop(conn, near_stop_id)
    lat, lon = conn.execute(
        "SELECT stop_lat, stop_lon FROM stops WHERE stop_id = ?", (centre.stop_id,)
    ).fetchone()
    if lat is None or lon is None:
        raise NotFoundError(f"{centre.stop_name} (id {centre.stop_id}) has no location in the timetable.")
    dlat = radius_m / _METRES_PER_DEGREE_LAT
    dlon = radius_m / (_METRES_PER_DEGREE_LAT * math.cos(math.radians(lat)))
    rows = conn.execute(
        f"""
        SELECT {_STOP_COLUMNS}, stop_lat, stop_lon FROM stops
        WHERE {' AND '.join(where)}
          AND stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ?
        """,
        [*params, lat - dlat, lat + dlat, lon - dlon, lon + dlon],
    ).fetchall()
    found = []
    for *cols, s_lat, s_lon in rows:
        d = _distance_m(lat, lon, s_lat, s_lon)
        if d <= radius_m:
            found.append(Stop(*cols, distance_m=round(d)))
    found.sort(key=lambda st: (st.distance_m, st.stop_name, st.stop_id))
    return found


def _with_towards(conn: sqlite3.Connection, stops: list[Stop]) -> list[Stop]:
    """Attach each stop's main headsigns from the precomputed stop_headsigns table."""
    if not stops:
        return stops
    placeholders = ",".join("?" * len(stops))
    towards: dict[str, list[str]] = {}
    for stop_id, headsign in conn.execute(
        f"SELECT stop_id, headsign FROM stop_headsigns WHERE stop_id IN ({placeholders}) "
        "ORDER BY stop_id, trips DESC, headsign",
        [st.stop_id for st in stops],
    ):
        heads = towards.setdefault(stop_id, [])
        if headsign and len(heads) < TOWARDS_SHOWN:
            heads.append(headsign)
    return [replace(st, towards=tuple(towards.get(st.stop_id, ()))) for st in stops]


def get_stop(conn: sqlite3.Connection, stop_id: str) -> Stop:
    """Return one boarding stop, or raise NotFoundError."""
    stop_id = validate_stop_id(stop_id)
    row = conn.execute(
        f"SELECT {_STOP_COLUMNS}, IFNULL(location_type, 0) FROM stops WHERE stop_id = ?",
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
    routes = [RouteSummary(rid, short, long_, _mode_name(rtype)) for rid, short, long_, rtype in rows]
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


def _local_time(base: datetime, secs: int) -> datetime:
    """A GTFS time (seconds since the service-day start `base`) as Adelaide local time."""
    return (base + timedelta(seconds=secs)).astimezone(ADELAIDE_TZ)


def next_departures(
    conn: sqlite3.Connection,
    stop_id: str,
    now: datetime,
    limit: int = 10,
    minutes_ahead: int = DEFAULT_MINUTES_AHEAD,
    route: str | None = None,
    to_stop_id: str | None = None,
) -> DepartureBoard:
    """Scheduled departures from a stop between `now` and `now + minutes_ahead`.

    Timetable only (no live delays). A trip belongs to the service day it
    started on, and its times can exceed 24:00 (e.g. 24:50 = 00:50 next
    morning). So for each of yesterday, today and tomorrow we convert the time
    window into that day's "seconds since service-day start" and query it.
    Yesterday catches late-night trips after midnight; tomorrow is only needed
    when the window reaches past midnight.

    Skipped: stops where you cannot board (pickup_type 1) and the last stop
    of each trip (the vehicle terminates there, so it is not a departure).

    With to_stop_id, only trips that call at that stop later (and let
    passengers off there, drop_off_type != 1) are kept, with their arrival
    time. Direct trips only: no transfers. If a trip visits the destination
    twice (loop routes), the first arrival after boarding is used.
    """
    stop = get_stop(conn, stop_id)
    limit = validate_limit(limit)
    minutes_ahead = validate_minutes_ahead(minutes_ahead)
    route = validate_route(route)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise TypeError("now must be a timezone-aware datetime")  # programming error, not user input

    if route is not None:
        _check_route_serves_stop(conn, route, stop)
    to_stop = None
    if to_stop_id is not None:
        to_stop = get_stop(conn, to_stop_id)
        if to_stop.stop_id == stop.stop_id:
            raise ValueError("to_stop_id must be a different stop from stop_id")

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
        params: list[object] = []
        if to_stop is not None:
            # First arrival at the destination after boarding, or NULL if the trip never gets there.
            arrival_sql = """(SELECT MIN(dst.arrival_secs) FROM stop_times dst
                              WHERE dst.trip_id = st.trip_id AND dst.stop_id = ?
                                AND dst.stop_sequence > st.stop_sequence
                                AND IFNULL(dst.drop_off_type, 0) != 1)"""
            params.append(to_stop.stop_id)
        else:
            arrival_sql = "NULL"
        params += [stop.stop_id, lo, hi, *sorted(services)]
        route_sql = ""
        if route is not None:
            route_sql = f"AND {_ROUTE_MATCH_SQL}"
            params += [route, route]
        reaches_sql = "WHERE arrival_secs IS NOT NULL" if to_stop is not None else ""
        params.append(limit)

        rows = conn.execute(
            f"""
            SELECT * FROM (
                SELECT st.departure_secs, IFNULL(r.route_short_name, ''), IFNULL(r.route_long_name, ''),
                       r.route_type, IFNULL(t.trip_headsign, ''), t.trip_id,
                       {arrival_sql} AS arrival_secs
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
            )
            {reaches_sql}
            ORDER BY departure_secs
            LIMIT ?
            """,
            params,
        ).fetchall()

        for secs, short, long_, rtype, headsign, trip_id, arr_secs in rows:
            departs_at = _local_time(base, secs)
            arrives_at = None if arr_secs is None else _local_time(base, arr_secs)
            departures.append(Departure(departs_at, short, long_, _mode_name(rtype), headsign, trip_id, arrives_at))

    if to_stop is not None and not departures:
        _check_direct_trip_exists(conn, stop, to_stop)

    departures.sort(key=lambda d: (d.departs_at, d.route_short_name, d.trip_id))
    return DepartureBoard(stop, to_stop, departures[:limit])


def _check_direct_trip_exists(conn: sqlite3.Connection, stop: Stop, to_stop: Stop) -> None:
    """Called only when no trips were found: is it "none soon" or "never"?

    "Never" (e.g. the wrong direction, or a transfer is needed) is an error,
    so Claude looks for another stop instead of telling the user to wait.
    """
    found = conn.execute(
        """
        SELECT 1 FROM stop_times a
        JOIN stop_times b ON b.trip_id = a.trip_id
        WHERE a.stop_id = ? AND b.stop_id = ?
          AND b.stop_sequence > a.stop_sequence
          AND IFNULL(a.pickup_type, 0) != 1 AND IFNULL(b.drop_off_type, 0) != 1
        LIMIT 1
        """,
        (stop.stop_id, to_stop.stop_id),
    ).fetchone()
    if found is None:
        raise NotFoundError(
            f"No trip goes directly from {stop.stop_name} (id {stop.stop_id}) to {to_stop.stop_name} "
            f"(id {to_stop.stop_id}) at any time: check the direction of travel (the stop on the other "
            "side of the road has a different id), or the journey needs a transfer."
        )


def _check_route_serves_stop(conn: sqlite3.Connection, route: str, stop: Stop) -> None:
    """Tell 'no such route' and 'route never stops here' apart from 'none soon'.

    Without this, asking for a tram at a bus stop would answer "no departures
    in the next 2 hours", which wrongly suggests waiting longer would help.
    """
    if conn.execute(f"SELECT 1 FROM routes r WHERE {_ROUTE_MATCH_SQL}", (route, route)).fetchone() is None:
        raise NotFoundError(f"No route {route!r}. Use routes_at_stop to see the routes at this stop.")
    serves = conn.execute(
        f"""
        SELECT 1 FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE st.stop_id = ? AND {_ROUTE_MATCH_SQL}
        LIMIT 1
        """,
        (stop.stop_id, route, route),
    ).fetchone()
    if serves is None:
        raise NotFoundError(
            f"Route {route} does not stop at {stop.stop_name} (id {stop.stop_id}). "
            "Use routes_at_stop to see the routes that do."
        )


def trip_stops(
    conn: sqlite3.Connection,
    trip_id: str,
    from_stop_id: str | None = None,
    max_stops: int = DEFAULT_MAX_STOPS,
) -> TripStops:
    """Every stop one trip calls at, in order, with its timetable time.

    This answers "where does this bus go?", which next_departures cannot:
    to_stop_id only checks a destination you already know. With
    from_stop_id, only the stops after that one are returned (the part of
    the trip you would ride). If a loop trip visits from_stop_id twice, the
    first visit where you can board is used.

    At most max_stops are listed. If the trip goes further, the number left
    out and the final stop are returned too, so the client still knows where
    it ends and can ask again with a bigger max_stops.
    """
    trip_id = validate_trip_id(trip_id)
    max_stops = validate_max_stops(max_stops)
    from_stop = get_stop(conn, from_stop_id) if from_stop_id is not None else None

    trip = conn.execute(
        """
        SELECT IFNULL(r.route_short_name, ''), IFNULL(r.route_long_name, ''), r.route_type,
               IFNULL(t.trip_headsign, '')
        FROM trips t JOIN routes r ON r.route_id = t.route_id
        WHERE t.trip_id = ?
        """,
        (trip_id,),
    ).fetchone()
    if trip is None:
        raise NotFoundError(f"No trip with id {trip_id!r}. Use next_departures to get a trip id.")
    short, long_, rtype, headsign = trip

    rows = conn.execute(
        """
        SELECT s.stop_id, IFNULL(s.stop_code, ''), s.stop_name, IFNULL(s.stop_desc, ''),
               st.stop_sequence, IFNULL(st.arrival_secs, st.departure_secs),
               IFNULL(st.pickup_type, 0) != 1, IFNULL(st.drop_off_type, 0) != 1
        FROM stop_times st JOIN stops s ON s.stop_id = st.stop_id
        WHERE st.trip_id = ?
        ORDER BY st.stop_sequence
        """,
        (trip_id,),
    ).fetchall()
    stops = [
        TripStop(Stop(sid, code, name, desc), seq, secs, bool(board), bool(alight))
        for sid, code, name, desc, seq, secs, board, alight in rows
    ]

    if from_stop is not None:
        start = next(
            (i for i, ts in enumerate(stops) if ts.stop.stop_id == from_stop.stop_id and ts.can_board),
            None,
        )
        if start is None:
            raise NotFoundError(
                f"Trip {trip_id} does not pick up at {from_stop.stop_name} (id {from_stop.stop_id}). "
                "Use the trip id from next_departures for this stop."
            )
        stops = stops[start + 1:]

    more, last = 0, None
    if len(stops) > max_stops:
        more, last = len(stops) - max_stops, stops[-1]
        stops = stops[:max_stops]
    return TripStops(trip_id, short, long_, _mode_name(rtype), headsign, from_stop, stops, more, last)


def feed_dates(conn: sqlite3.Connection) -> tuple[date | None, date | None]:
    """The first and last dates the loaded timetable covers (None when the feed does not say)."""
    meta = dict(conn.execute(
        "SELECT key, value FROM metadata WHERE key IN ('feed_start_date', 'feed_end_date')"
    ))
    return _parse_gtfs_date(meta.get("feed_start_date")), _parse_gtfs_date(meta.get("feed_end_date"))


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
