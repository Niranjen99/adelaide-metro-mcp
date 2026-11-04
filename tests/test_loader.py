"""Tests for the GTFS -> SQLite loader. No network: downloads are replaced."""

import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from adelaide_metro_mcp import gtfs_loader
from adelaide_metro_mcp.gtfs_loader import (
    GTFSFormatError,
    build_database,
    read_loaded_version,
    time_to_seconds,
)
from conftest import FIXTURE_DIR, make_zip, set_schema_version


def query(db: Path, sql: str, *params):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute(sql, params).fetchall()


# --- time parsing ---------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("00:00:00", 0),
        ("05:52:00", 5 * 3600 + 52 * 60),
        ("5:52:00", 5 * 3600 + 52 * 60),      # GTFS allows a single-digit hour
        ("25:10:00", 25 * 3600 + 10 * 60),    # after midnight, same service day
        ("", None),                            # allowed for non-timepoint stops
    ],
)
def test_time_to_seconds(text, expected):
    assert time_to_seconds(text) == expected


@pytest.mark.parametrize("bad", ["8am", "08:60:00", "08:00", "aa:bb:cc"])
def test_time_to_seconds_rejects_bad_values(bad):
    with pytest.raises(GTFSFormatError):
        time_to_seconds(bad)


# --- building the database ------------------------------------------------

def test_build_loads_expected_row_counts(tmp_path, gtfs_zip):
    counts = build_database(gtfs_zip, tmp_path / "gtfs.sqlite")
    assert counts == {
        "stops": 4,  # 3 boarding stops + parent station P1
        "routes": 2,
        "trips": 5,
        "stop_times": 13,
        "calendar": 2,
        "calendar_dates": 2,
    }


def test_after_midnight_times_are_kept_past_24h(gtfs_db):
    rows = query(gtfs_db, "SELECT departure_secs FROM stop_times WHERE trip_id='T4' ORDER BY stop_sequence")
    assert rows == [(24 * 3600 + 50 * 60,), (25 * 3600 + 10 * 60,)]


def test_metadata_comes_from_feed_info(gtfs_db):
    meta = dict(query(gtfs_db, "SELECT key, value FROM metadata"))
    assert meta["feed_version"] == "999"
    assert meta["feed_start_date"] == "20260101"
    assert meta["feed_end_date"] == "20271231"
    assert read_loaded_version(gtfs_db) == "999"


def test_explicit_version_overrides_feed_info(tmp_path, gtfs_zip):
    db = tmp_path / "gtfs.sqlite"
    build_database(gtfs_zip, db, version="1714")
    assert read_loaded_version(db) == "1714"


def test_empty_optional_values_become_null(gtfs_db):
    # S2 has no wheelchair_boarding value in the fixture
    assert query(gtfs_db, "SELECT wheelchair_boarding FROM stops WHERE stop_id='S2'") == [(None,)]


def test_index_on_stop_and_departure_exists(gtfs_db):
    names = {r[0] for r in query(gtfs_db, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_stop_times_stop_dep" in names


def test_utf8_bom_in_header_is_handled(tmp_path):
    # Some GTFS producers write a byte-order mark; the first column must still be found.
    src = tmp_path / "bom"
    src.mkdir()
    for f in FIXTURE_DIR.glob("*.txt"):
        data = f.read_bytes()
        if f.name == "stops.txt":
            data = b"\xef\xbb\xbf" + data
        (src / f.name).write_bytes(data)
    counts = build_database(make_zip(src, tmp_path / "g.zip"), tmp_path / "g.sqlite")
    assert counts["stops"] == 4


# --- bad input --------------------------------------------------------------

def test_missing_required_file_raises(tmp_path):
    z = make_zip(FIXTURE_DIR, tmp_path / "g.zip", skip={"stop_times.txt"})
    with pytest.raises(GTFSFormatError, match="stop_times.txt"):
        build_database(z, tmp_path / "g.sqlite")


def test_missing_required_column_raises(tmp_path):
    z = tmp_path / "g.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in FIXTURE_DIR.glob("*.txt"):
            text = f.read_text()
            if f.name == "stops.txt":
                text = text.replace("stop_name", "name_of_stop", 1)
            zf.writestr(f.name, text)
    with pytest.raises(GTFSFormatError, match="stop_name"):
        build_database(z, tmp_path / "g.sqlite")


def test_failed_build_keeps_previous_database(tmp_path, gtfs_zip):
    db = tmp_path / "gtfs.sqlite"
    build_database(gtfs_zip, db, version="1")
    bad = make_zip(FIXTURE_DIR, tmp_path / "bad.zip", skip={"trips.txt"})
    with pytest.raises(GTFSFormatError):
        build_database(bad, db, version="2")
    assert read_loaded_version(db) == "1"                   # old data still served
    assert not list(tmp_path.glob("*.sqlite.tmp"))          # temp file cleaned up


# --- update(): version check, with network functions replaced ---------------

@pytest.fixture
def fake_network(monkeypatch, gtfs_zip):
    """Replace the two network calls; record how many downloads happen."""
    state = {"version": "1714", "downloads": 0}

    def fake_version():
        return state["version"]

    def fake_download(dest):
        state["downloads"] += 1
        Path(dest).write_bytes(gtfs_zip.read_bytes())

    monkeypatch.setattr(gtfs_loader, "fetch_latest_version", fake_version)
    monkeypatch.setattr(gtfs_loader, "download_zip", fake_download)
    return state


def test_update_downloads_when_no_database(tmp_path, fake_network):
    msg = gtfs_loader.update(tmp_path)
    assert fake_network["downloads"] == 1
    assert "version 1714" in msg
    assert read_loaded_version(tmp_path / "gtfs.sqlite") == "1714"


def test_update_skips_when_version_unchanged(tmp_path, fake_network):
    gtfs_loader.update(tmp_path)
    msg = gtfs_loader.update(tmp_path)
    assert fake_network["downloads"] == 1
    assert "up to date" in msg


def test_update_reloads_when_new_version_published(tmp_path, fake_network):
    gtfs_loader.update(tmp_path)
    fake_network["version"] = "1715"
    gtfs_loader.update(tmp_path)
    assert fake_network["downloads"] == 2
    assert read_loaded_version(tmp_path / "gtfs.sqlite") == "1715"


def test_update_force_always_downloads(tmp_path, fake_network):
    gtfs_loader.update(tmp_path)
    gtfs_loader.update(tmp_path, force=True)
    assert fake_network["downloads"] == 2


# --- schema version: rebuild databases made by an older version of the loader --------

def test_schema_version_is_stored(gtfs_db):
    meta = dict(query(gtfs_db, "SELECT key, value FROM metadata"))
    assert meta["schema_version"] == gtfs_loader.SCHEMA_VERSION


def test_drop_off_type_is_loaded(gtfs_db):
    rows = query(gtfs_db, "SELECT drop_off_type FROM stop_times WHERE trip_id='T2' AND stop_id='S2'")
    assert rows == [(1,)]


def test_old_schema_counts_as_not_loaded(gtfs_db):
    set_schema_version(gtfs_db, "1")
    assert read_loaded_version(gtfs_db) is None


def test_update_rebuilds_old_schema_even_if_timetable_unchanged(tmp_path, fake_network):
    gtfs_loader.update(tmp_path)
    set_schema_version(tmp_path / "gtfs.sqlite", "1")
    gtfs_loader.update(tmp_path)
    assert fake_network["downloads"] == 2
    assert read_loaded_version(tmp_path / "gtfs.sqlite") == "1714"


def test_stop_headsigns_are_precomputed(gtfs_db):
    rows = query(gtfs_db, "SELECT stop_id, headsign, trips FROM stop_headsigns ORDER BY stop_id, trips DESC")
    # Boardable, not-last-stop calls only: T5 is set-down only at S2, and S3 ends every trip.
    assert rows == [
        ("S1", "Beach Interchange", 4),
        ("S1", "Beach", 1),
        ("S2", "Beach Interchange", 2),
    ]
