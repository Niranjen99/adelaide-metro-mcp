"""Download the Adelaide Metro static GTFS timetable and load it into SQLite.

Usage (from the project folder, with the virtual environment active):

    python -m adelaide_metro_mcp.gtfs_loader          # download only if a new version exists
    python -m adelaide_metro_mcp.gtfs_loader --force  # always re-download and rebuild

Design notes:
- Only the files and columns the tools need are loaded (shapes.txt alone is
  36 MB and is skipped).
- GTFS times can go past midnight ("25:10:00" = 1:10am on the next calendar
  day, still the same service day), so times are stored as seconds since
  the start of the service day, not as clock strings.
- The database is built in a temporary file and then swapped in, so a crash
  or bad download never leaves the server with a half-built database.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.request
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Verified against https://gtfs.adelaidemetro.com.au/gtfsr-feed-v1-swagger-apigateway.yaml
BASE_URL = "https://gtfs.adelaidemetro.com.au/v1"
STATIC_ZIP_URL = f"{BASE_URL}/static/latest/google_transit.zip"
VERSION_URL = f"{BASE_URL}/static/latest/version.txt"

USER_AGENT = "adelaide-metro-mcp/0.1 (+https://github.com/Niranjen99)"
HTTP_TIMEOUT_SECONDS = 60

DB_FILENAME = "gtfs.sqlite"
ZIP_FILENAME = "google_transit.zip"

# Bump whenever SCHEMA_SQL or TABLES change. A database built with a different
# schema version is rebuilt by update() even if the timetable version is the same,
# and the server refuses to use it (instead of failing on a missing column).
SCHEMA_VERSION = "3"  # 2: stop_times.drop_off_type; 3: stop_headsigns table

_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")


class GTFSFormatError(ValueError):
    """The GTFS zip is missing a required file or column, or has a bad value."""


def default_data_dir() -> Path:
    """Folder for the downloaded zip and the database.

    Override with the ADELAIDE_METRO_DATA_DIR environment variable.
    """
    env = os.environ.get("ADELAIDE_METRO_DATA_DIR")
    return Path(env) if env else Path.cwd() / "data"


def time_to_seconds(value: str) -> int | None:
    """Convert a GTFS time like "25:10:00" to seconds since the start of the service day.

    Empty values are allowed by GTFS for non-timepoint stops and return None.
    """
    value = value.strip()
    if not value:
        return None
    m = _TIME_RE.match(value)
    if not m:
        raise GTFSFormatError(f"Invalid GTFS time: {value!r}")
    h, mnt, s = (int(g) for g in m.groups())
    return h * 3600 + mnt * 60 + s


# ---------------------------------------------------------------------------
# Schema: table -> (source file, [(sql column, gtfs column, required, converter)])
# ---------------------------------------------------------------------------

def _int_or_none(v: str) -> int | None:
    v = v.strip()
    return int(v) if v else None


def _float_or_none(v: str) -> float | None:
    v = v.strip()
    return float(v) if v else None


def _text(v: str) -> str:
    return v.strip()


TABLES: dict[str, tuple[str, list[tuple[str, str, bool, object]]]] = {
    "stops": ("stops.txt", [
        ("stop_id", "stop_id", True, _text),
        ("stop_code", "stop_code", False, _text),
        ("stop_name", "stop_name", True, _text),
        ("stop_desc", "stop_desc", False, _text),
        ("stop_lat", "stop_lat", False, _float_or_none),
        ("stop_lon", "stop_lon", False, _float_or_none),
        ("location_type", "location_type", False, _int_or_none),
        ("parent_station", "parent_station", False, _text),
        ("wheelchair_boarding", "wheelchair_boarding", False, _int_or_none),
    ]),
    "routes": ("routes.txt", [
        ("route_id", "route_id", True, _text),
        ("agency_id", "agency_id", False, _text),
        ("route_short_name", "route_short_name", False, _text),
        ("route_long_name", "route_long_name", False, _text),
        ("route_type", "route_type", True, _int_or_none),
    ]),
    "trips": ("trips.txt", [
        ("trip_id", "trip_id", True, _text),
        ("route_id", "route_id", True, _text),
        ("service_id", "service_id", True, _text),
        ("trip_headsign", "trip_headsign", False, _text),
        ("direction_id", "direction_id", False, _int_or_none),
        ("wheelchair_accessible", "wheelchair_accessible", False, _int_or_none),
    ]),
    "stop_times": ("stop_times.txt", [
        ("trip_id", "trip_id", True, _text),
        ("stop_id", "stop_id", True, _text),
        ("stop_sequence", "stop_sequence", True, _int_or_none),
        ("arrival_secs", "arrival_time", True, time_to_seconds),
        ("departure_secs", "departure_time", True, time_to_seconds),
        ("pickup_type", "pickup_type", False, _int_or_none),
        ("drop_off_type", "drop_off_type", False, _int_or_none),
    ]),
    "calendar": ("calendar.txt", [
        ("service_id", "service_id", True, _text),
        ("monday", "monday", True, _int_or_none),
        ("tuesday", "tuesday", True, _int_or_none),
        ("wednesday", "wednesday", True, _int_or_none),
        ("thursday", "thursday", True, _int_or_none),
        ("friday", "friday", True, _int_or_none),
        ("saturday", "saturday", True, _int_or_none),
        ("sunday", "sunday", True, _int_or_none),
        ("start_date", "start_date", True, _text),
        ("end_date", "end_date", True, _text),
    ]),
    "calendar_dates": ("calendar_dates.txt", [
        ("service_id", "service_id", True, _text),
        ("date", "date", True, _text),
        ("exception_type", "exception_type", True, _int_or_none),
    ]),
}

SCHEMA_SQL = """
CREATE TABLE stops (
    stop_id TEXT PRIMARY KEY, stop_code TEXT, stop_name TEXT NOT NULL,
    stop_desc TEXT, stop_lat REAL, stop_lon REAL, location_type INTEGER,
    parent_station TEXT, wheelchair_boarding INTEGER
);
CREATE TABLE routes (
    route_id TEXT PRIMARY KEY, agency_id TEXT, route_short_name TEXT,
    route_long_name TEXT, route_type INTEGER NOT NULL
);
CREATE TABLE trips (
    trip_id TEXT PRIMARY KEY, route_id TEXT NOT NULL, service_id TEXT NOT NULL,
    trip_headsign TEXT, direction_id INTEGER, wheelchair_accessible INTEGER
);
CREATE TABLE stop_times (
    trip_id TEXT NOT NULL, stop_id TEXT NOT NULL, stop_sequence INTEGER NOT NULL,
    arrival_secs INTEGER, departure_secs INTEGER, pickup_type INTEGER,
    drop_off_type INTEGER
);
CREATE TABLE calendar (
    service_id TEXT PRIMARY KEY, monday INTEGER, tuesday INTEGER,
    wednesday INTEGER, thursday INTEGER, friday INTEGER, saturday INTEGER,
    sunday INTEGER, start_date TEXT NOT NULL, end_date TEXT NOT NULL
);
CREATE TABLE calendar_dates (
    service_id TEXT NOT NULL, date TEXT NOT NULL, exception_type INTEGER NOT NULL
);
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
"""

# Indexes are created after the bulk insert (faster than updating them per row).
INDEX_SQL = """
CREATE INDEX idx_stop_times_stop_dep ON stop_times (stop_id, departure_secs);
CREATE INDEX idx_stop_times_trip ON stop_times (trip_id);
CREATE INDEX idx_trips_route ON trips (route_id);
CREATE INDEX idx_stops_code ON stops (stop_code);
CREATE INDEX idx_calendar_dates_date ON calendar_dates (date);
"""

# Derived data, built once after loading (too slow to compute per request: up to
# 80 ms per busy stop). For each stop: where its departing trips are headed, and
# how many trips go there. find_stops shows the top two as "towards ...", so the
# client can tell the two sides of a road apart without calling next_departures.
# Same rules as a departure: you can board (pickup_type != 1) and it is not the
# trip's last stop.
DERIVED_SQL = """
CREATE TABLE stop_headsigns AS
WITH last_stop AS (
    SELECT trip_id, MAX(stop_sequence) AS last_seq FROM stop_times GROUP BY trip_id
)
SELECT st.stop_id,
       COALESCE(NULLIF(t.trip_headsign, ''), r.route_long_name, '') AS headsign,
       COUNT(*) AS trips
FROM stop_times st
JOIN last_stop l ON l.trip_id = st.trip_id AND st.stop_sequence < l.last_seq
JOIN trips t ON t.trip_id = st.trip_id
JOIN routes r ON r.route_id = t.route_id
WHERE IFNULL(st.pickup_type, 0) != 1
GROUP BY st.stop_id, headsign;
CREATE INDEX idx_stop_headsigns_stop ON stop_headsigns (stop_id, trips DESC);
"""

BATCH_SIZE = 50_000


def _read_rows(zf: zipfile.ZipFile, filename: str):
    """Yield dict rows from a CSV file inside the zip (handles a UTF-8 BOM)."""
    try:
        raw = zf.open(filename)
    except KeyError:
        raise GTFSFormatError(f"GTFS zip is missing required file {filename}") from None
    with raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
        reader = csv.DictReader(text)
        yield reader.fieldnames or []
        yield from reader


def _load_table(conn: sqlite3.Connection, zf: zipfile.ZipFile, table: str) -> int:
    filename, columns = TABLES[table]
    rows = _read_rows(zf, filename)
    header = next(rows)

    missing = [g for _, g, required, _ in columns if required and g not in header]
    if missing:
        raise GTFSFormatError(f"{filename} is missing required column(s): {', '.join(missing)}")

    present = [(sql, g, conv) for sql, g, _, conv in columns if g in header]
    sql_cols = ", ".join(c for c, _, _ in present)
    placeholders = ", ".join("?" for _ in present)
    insert = f"INSERT INTO {table} ({sql_cols}) VALUES ({placeholders})"

    count = 0
    batch: list[tuple] = []
    for line_no, row in enumerate(rows, start=2):
        try:
            batch.append(tuple(conv(row[g] or "") for _, g, conv in present))
        except (ValueError, GTFSFormatError) as exc:
            raise GTFSFormatError(f"{filename} line {line_no}: {exc}") from exc
        if len(batch) >= BATCH_SIZE:
            conn.executemany(insert, batch)
            count += len(batch)
            batch.clear()
    if batch:
        conn.executemany(insert, batch)
        count += len(batch)
    return count


def _read_feed_info(zf: zipfile.ZipFile) -> dict[str, str]:
    """feed_info.txt is optional in GTFS; return its first row or {}."""
    if "feed_info.txt" not in zf.namelist():
        return {}
    rows = _read_rows(zf, "feed_info.txt")
    next(rows)  # header
    return {k: (v or "").strip() for k, v in next(rows, {}).items()}


def build_database(zip_path: Path, db_path: Path, version: str | None = None) -> dict[str, int]:
    """Build a fresh SQLite database from a GTFS zip, replacing db_path atomically.

    Returns the number of rows loaded per table.
    """
    zip_path, db_path = Path(zip_path), Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(suffix=".sqlite.tmp", dir=db_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(tmp_path)
        try:
            # Safe here: if anything fails, the temp file is thrown away.
            conn.execute("PRAGMA journal_mode = OFF")
            conn.execute("PRAGMA synchronous = OFF")
            conn.executescript(SCHEMA_SQL)
            with zipfile.ZipFile(zip_path) as zf:
                for table in TABLES:
                    counts[table] = _load_table(conn, zf, table)
                    log.info("Loaded %s: %d rows", table, counts[table])
                feed_info = _read_feed_info(zf)
            conn.executescript(INDEX_SQL)
            conn.executescript(DERIVED_SQL)
            meta = {
                "feed_version": version or feed_info.get("feed_version", ""),
                "schema_version": SCHEMA_VERSION,
                "feed_start_date": feed_info.get("feed_start_date", ""),
                "feed_end_date": feed_info.get("feed_end_date", ""),
                "loaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            conn.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", meta.items())
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp_path, db_path)  # atomic swap on the same drive
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return counts


def read_loaded_version(db_path: Path) -> str | None:
    """Return the feed version stored in an existing database, or None.

    Also None if the database was built with a different SCHEMA_VERSION, so
    update() rebuilds it.
    """
    if not Path(db_path).exists():
        return None
    try:
        # closing(): `with sqlite3.connect()` alone commits but does NOT close,
        # and Windows cannot replace a database file that is still open.
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            meta = dict(conn.execute(
                "SELECT key, value FROM metadata WHERE key IN ('feed_version', 'schema_version')"
            ))
    except sqlite3.Error:
        return None
    if meta.get("schema_version") != SCHEMA_VERSION:
        return None
    return meta.get("feed_version")


# ---------------------------------------------------------------------------
# Network (kept small and separate so tests can replace these two functions)
# ---------------------------------------------------------------------------

def _open_url(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS)


def fetch_latest_version() -> str:
    with _open_url(VERSION_URL) as resp:
        return resp.read().decode("utf-8").strip()


def download_zip(dest: Path) -> None:
    tmp = Path(dest).with_suffix(".part")
    with _open_url(STATIC_ZIP_URL) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise GTFSFormatError("Downloaded file is not a valid zip")
    os.replace(tmp, dest)


def update(data_dir: Path | None = None, force: bool = False) -> str:
    """Download and load the timetable if a newer version is published.

    Returns a one-line summary of what happened.
    """
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / DB_FILENAME
    zip_path = data_dir / ZIP_FILENAME

    latest = fetch_latest_version()
    current = read_loaded_version(db_path)
    if not force and current == latest:
        return f"Timetable already up to date (version {current})."

    started = time.perf_counter()
    download_zip(zip_path)
    counts = build_database(zip_path, db_path, version=latest)
    elapsed = time.perf_counter() - started
    return (
        f"Loaded timetable version {latest} into {db_path} in {elapsed:.1f}s "
        f"({counts['stops']} stops, {counts['routes']} routes, "
        f"{counts['trips']} trips, {counts['stop_times']} stop times)."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Download the Adelaide Metro GTFS timetable into SQLite.")
    parser.add_argument("--force", action="store_true", help="re-download even if the version is unchanged")
    parser.add_argument("--data-dir", type=Path, default=None, help="where to store the zip and database")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(update(args.data_dir, force=args.force))


if __name__ == "__main__":
    main()
