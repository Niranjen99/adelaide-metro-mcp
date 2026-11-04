"""Measure query speed on the real Adelaide Metro database.

Usage (from the project folder, after downloading the timetable):

    python benchmarks/bench_queries.py            # query timings
    python benchmarks/bench_queries.py --rebuild  # also time a full database build from the zip

Prints a Markdown table you can paste into the README. All timings are
wall-clock on this machine, so quote the machine with the numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import random
import statistics
import sys
import tempfile
import time
from contextlib import closing
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from adelaide_metro_mcp import db
from adelaide_metro_mcp.gtfs_loader import ZIP_FILENAME, build_database, default_data_dir

SEED = 42  # fixed, so every run benchmarks the same stops and times
WARMUP_RUNS = 20  # untimed runs first, so caches are warm


def summarise(samples_ms: list[float]) -> dict[str, float]:
    """Median / p95 / max of a list of timings in milliseconds."""
    samples_ms = sorted(samples_ms)
    return {
        "median": statistics.median(samples_ms),
        "p95": samples_ms[int(len(samples_ms) * 0.95) - 1],
        "max": samples_ms[-1],
    }


def measure(fn, runs: int) -> dict[str, float]:
    """Run fn() WARMUP_RUNS times untimed, then `runs` times timed."""
    for _ in range(WARMUP_RUNS):
        fn()
    samples = []
    for _ in range(runs):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return summarise(samples)


def first_weekday_on_or_after(d: date, weekday: int) -> date:
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def busiest_stop(conn) -> str:
    """The stop with the most scheduled calls: the worst case for departure queries."""
    return conn.execute(
        "SELECT stop_id FROM stop_times GROUP BY stop_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]


def downstream_stop(conn, stop_id: str, hops: int = 5) -> str:
    """A stop some trip reaches `hops` stops after stop_id: a realistic destination."""
    return conn.execute(
        """SELECT b.stop_id FROM stop_times a JOIN stop_times b ON b.trip_id = a.trip_id
           WHERE a.stop_id = ? AND b.stop_sequence = a.stop_sequence + ? LIMIT 1""",
        (stop_id, hops),
    ).fetchone()[0]


def longest_trip(conn) -> str:
    """The trip with the most stops: the worst case for trip_stops."""
    return conn.execute(
        "SELECT trip_id FROM stop_times GROUP BY trip_id ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]


def bench_queries(conn, busiest: str, runs: int) -> list[tuple[str, dict[str, float]]]:
    info = db.timetable_info(conn)
    if info.start_date is None:
        sys.exit("Timetable has no start date; rebuild it with the loader.")
    # A Wednesday inside the timetable's validity period, 08:00 (morning peak).
    wednesday = first_weekday_on_or_after(max(info.start_date, date.today()), 2)
    peak = datetime.combine(wednesday, dtime(8, 0), tzinfo=db.ADELAIDE_TZ)

    rng = random.Random(SEED)
    stop_ids = [r[0] for r in conn.execute(
        "SELECT stop_id FROM stops WHERE IFNULL(location_type, 0) = 0 ORDER BY stop_id"
    )]
    random_cases = [
        (rng.choice(stop_ids), peak.replace(hour=rng.randrange(5, 24), minute=rng.randrange(60)))
        for _ in range(runs)
    ]
    cases = iter(random_cases * 2)  # enough for warm-up + timed runs
    destination = downstream_stop(conn, busiest)
    long_trip = longest_trip(conn)
    n_stops = len(db.trip_stops(conn, long_trip, max_stops=db.MAX_MAX_STOPS).stops)

    def random_stop_departures():
        stop_id, now = next(cases)
        db.next_departures(conn, stop_id, now)

    return [
        ("find_stops('king william')", measure(lambda: db.find_stops(conn, "king william"), runs)),
        ("find_stops near_stop_id (busiest stop, 400 m)", measure(lambda: db.find_stops(conn, near_stop_id=busiest), runs)),
        (f"routes_at_stop (busiest stop, id {busiest})", measure(lambda: db.routes_at_stop(conn, busiest), runs)),
        ("next_departures (busiest stop, Wed 08:00)", measure(lambda: db.next_departures(conn, busiest, peak), runs)),
        ("next_departures (random stop and time)", measure(random_stop_departures, runs)),
        ("next_departures with to_stop_id (busiest stop, Wed 08:00)",
         measure(lambda: db.next_departures(conn, busiest, peak, to_stop_id=destination), runs)),
        (f"trip_stops (longest trip, all {n_stops} stops)",
         measure(lambda: db.trip_stops(conn, long_trip, max_stops=db.MAX_MAX_STOPS), runs)),
    ]


def bench_mcp_round_trip(stop_id: str, runs: int) -> dict[str, float]:
    """Full tool call through an in-process MCP client (JSON-RPC, validation, formatting)."""
    from mcp import Client

    from adelaide_metro_mcp.server import mcp

    async def go():
        async with Client(mcp) as client:
            for _ in range(WARMUP_RUNS):
                await client.call_tool("next_departures", {"stop_id": stop_id})
            samples = []
            for _ in range(runs):
                start = time.perf_counter_ns()
                await client.call_tool("next_departures", {"stop_id": stop_id})
                samples.append((time.perf_counter_ns() - start) / 1e6)
        return summarise(samples)

    return asyncio.run(go())


def bench_rebuild() -> float:
    zip_path = default_data_dir() / ZIP_FILENAME
    if not zip_path.exists():
        sys.exit(f"{zip_path} not found. Run: python -m adelaide_metro_mcp.gtfs_loader")
    with tempfile.TemporaryDirectory() as tmp:
        start = time.perf_counter()
        build_database(zip_path, Path(tmp) / "bench.sqlite")
        return time.perf_counter() - start


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=1000, help="timed runs per query (default 1000)")
    parser.add_argument("--rebuild", action="store_true", help="also time a full database build from the zip")
    args = parser.parse_args()

    try:
        conn_cm = closing(db.connect())
    except db.DatabaseNotReadyError as exc:
        sys.exit(str(exc))

    with conn_cm as conn:
        info = db.timetable_info(conn)
        stop_times = conn.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0]
        busiest = busiest_stop(conn)
        results = bench_queries(conn, busiest, args.runs)
    results.append(("next_departures via MCP client (end to end)", bench_mcp_round_trip(busiest, min(args.runs, 300))))

    print(f"Timetable version {info.feed_version}: {info.stop_count} stops, {info.route_count} routes, "
          f"{info.trip_count} trips, {stop_times} stop times")
    print(f"Machine: {platform.processor() or platform.machine()}, {platform.system()} {platform.release()}, "
          f"Python {platform.python_version()}, {args.runs} runs per query\n")
    print("| Query | Median (ms) | p95 (ms) | Max (ms) |")
    print("|---|---:|---:|---:|")
    for name, r in results:
        print(f"| {name} | {r['median']:.2f} | {r['p95']:.2f} | {r['max']:.2f} |")

    if args.rebuild:
        print(f"\nFull database build from {ZIP_FILENAME}: {bench_rebuild():.1f} s")


if __name__ == "__main__":
    main()
