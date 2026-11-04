"""Read-only queries against the GTFS SQLite database.

Kept separate from the MCP layer so the logic is testable with plain pytest.
Every function takes an open sqlite3 connection and returns plain data;
formatting for humans happens in server.py.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from adelaide_metro_mcp.gtfs_loader import DB_FILENAME, default_data_dir

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
_STOP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")


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
