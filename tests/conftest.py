"""Shared pytest fixtures. Everything is built from local files: no network."""

import zipfile
from pathlib import Path

import pytest

from adelaide_metro_mcp.gtfs_loader import build_database

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mini_gtfs"


def make_zip(src_dir: Path, dest: Path, skip: set[str] | None = None) -> Path:
    """Zip every .txt file in src_dir (optionally skipping some) into dest."""
    skip = skip or set()
    with zipfile.ZipFile(dest, "w") as zf:
        for f in sorted(src_dir.glob("*.txt")):
            if f.name not in skip:
                zf.write(f, f.name)
    return dest


@pytest.fixture
def gtfs_zip(tmp_path: Path) -> Path:
    """The mini GTFS fixture packed as a zip, like the real download."""
    return make_zip(FIXTURE_DIR, tmp_path / "google_transit.zip")


@pytest.fixture
def gtfs_db(tmp_path: Path, gtfs_zip: Path) -> Path:
    """A SQLite database built from the mini GTFS fixture."""
    db_path = tmp_path / "gtfs.sqlite"
    build_database(gtfs_zip, db_path)
    return db_path


@pytest.fixture
def conn(gtfs_db: Path):
    """An open read-only connection to the fixture database."""
    from adelaide_metro_mcp import db

    c = db.connect(gtfs_db)
    yield c
    c.close()


@pytest.fixture
def server_data(monkeypatch, gtfs_db: Path) -> Path:
    """Point the MCP server at the fixture database (via its env variable)."""
    monkeypatch.setenv("ADELAIDE_METRO_DATA_DIR", str(gtfs_db.parent))
    return gtfs_db
