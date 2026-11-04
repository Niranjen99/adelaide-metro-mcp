# Adelaide Metro MCP server

[![CI](https://github.com/Niranjen99/adelaide-metro-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Niranjen99/adelaide-metro-mcp/actions/workflows/ci.yml)

An MCP server that lets Claude (or any MCP client) answer Adelaide Metro questions, such as "when's the next tram from City South?", using the official GTFS timetable.

**Tech stack:** Python 3.10+, the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (v2, stdio transport), SQLite, pytest, GitHub Actions.

<!-- TODO: add a screenshot of a real Claude conversation here, e.g. docs/demo.png -->

## What it can do

| Tool | Example question | What it returns |
|---|---|---|
| `find_stops` | "Find stops on King William St" / "What's across the road from this stop?" | Matching stops (by text, or within a radius of another stop) with the stop number, address and where its buses head ("towards City, West Lakes") |
| `routes_at_stop` | "Which routes stop at City South?" | Every bus, train and tram route that calls there |
| `next_departures` | "When's the next bus from stop D2?" / "Which buses go from Sefton Park to King William St?" | Scheduled departures in the next 2 hours (adjustable), optionally for one route or only trips that go directly to a destination stop, with arrival times |
| `trip_stops` | "Where does this bus go? Where do I get off for Hindley St?" | The stops one trip calls at after your stop (25 by default, the final stop always named), in order, with times, marking stops where you can't get off |
| `timetable_info` | "How current is your timetable?" | Timetable version, dates it's valid for, size |

All tools are read-only and validate their inputs. Departures come from the **published timetable only**. Live delays and service disruptions are planned (see [Roadmap](#roadmap)).

Example `next_departures` output (real data, timetable version 1714):

```
Scheduled departures from Stop D2 King William St - East side (stop 16286, id 6455) - 96 King William St, Adelaide
As of Wed 04 Nov 15:41 (timetable only, no live delays):
- 15:44  Bus T721 to Noarlunga (in 3 min), trip 1174247
- 15:54  Bus 721X to Noarlunga (in 13 min), trip 1173546
- 15:54  Bus T722 to Noarlunga (in 13 min), trip 1174304
- 15:55  Bus 719 to Flinders University (in 14 min), trip 1173415
```

With a destination (`to_stop_id`), from a real test asking Claude how to get from Sefton Park to Hindley St:

```
Scheduled direct trips from Stop 17 Main North Rd - East side (stop 12297, id 2393) - 255 Main North Rd, Sefton Park to Stop B1 King William St - East side (stop 13301, id 3414) - 12 King William St, Adelaide
As of Wed 04 Nov 16:38 (timetable only, no live delays):
- 16:40  Bus 222 to City (in 2 min), arrives 17:00 (20 min ride), trip 1178543
- 16:41  Bus 228F to City (in 3 min), arrives 16:58 (17 min ride), trip 1178742
- 16:54  Bus 222 to City (in 16 min), arrives 17:14 (20 min ride), trip 1178540
```

The 228F leaves a minute later but arrives two minutes earlier. Without the destination, Claude could list departures but couldn't tell which buses reach the city stop or when.

When the user only names a street ("Hindley St"), Claude may not know which stop the bus uses. `trip_stops` shows where a trip goes, so Claude can pick the stop nearest the destination (shortened, real data):

```
Bus 228F to City (trip 1178729)
Stops after Stop 17 Main North Rd - East side (stop 12297, id 2393) - 255 Main North Rd, Sefton Park, timetable times:
- 17:12  Stop 16 Main North Rd - East side (stop 12349, id 2445) - Sefton Plaza Shopping Centre, Prospect (set down only)
...
- 17:29  Stop B1 King William St - East side (stop 13301, id 3414) - 12 King William St, Adelaide
- 17:31  Stop D2 King William St - East side (stop 16286, id 6455) - 96 King William St, Adelaide
- 17:34  Stop VS3 Victoria Sq - East side (stop 13474, id 3589) - 242 Victoria Square, Adelaide (last stop)
```

<!-- TODO: add a screenshot of the conversation -->

## Quick start (Windows, Command Prompt)

```bat
git clone https://github.com/Niranjen99/adelaide-metro-mcp.git
cd adelaide-metro-mcp
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

:: Download the timetable (15 MB) and build data\gtfs.sqlite. Re-run any time: it skips the
:: download if the published version hasn't changed. Add --force to always rebuild.
.venv\Scripts\python.exe -m adelaide_metro_mcp.gtfs_loader

:: Run the tests (no network or downloaded data needed)
.venv\Scripts\python.exe -m pytest
```

On Linux/macOS, use `python3 -m venv .venv` and `.venv/bin/python` instead.

## Connect it to Claude

Claude starts the server itself. It doesn't start it in the project folder, so you have to tell the server where the data is with `ADELAIDE_METRO_DATA_DIR`. Replace `C:\path\to\adelaide-metro-mcp` with your clone's full path.

**Claude Code:**

```bat
claude mcp add adelaide-metro -e ADELAIDE_METRO_DATA_DIR="C:\path\to\adelaide-metro-mcp\data" -- "C:\path\to\adelaide-metro-mcp\.venv\Scripts\python.exe" -m adelaide_metro_mcp
```

**Claude Desktop:** add this to `%APPDATA%\Claude\claude_desktop_config.json`, then restart Claude Desktop:

```json
{
  "mcpServers": {
    "adelaide-metro": {
      "command": "C:\\path\\to\\adelaide-metro-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "adelaide_metro_mcp"],
      "env": { "ADELAIDE_METRO_DATA_DIR": "C:\\path\\to\\adelaide-metro-mcp\\data" }
    }
  }
}
```

## Performance

Measured with `benchmarks\bench_queries.py` on the full Adelaide Metro timetable (9,038 stops, 717 routes, 22,208 trips, 812,881 stop times):

```bat
.venv\Scripts\python.exe benchmarks\bench_queries.py --rebuild
```

Results on a Windows 11 desktop (AMD64, Python 3.12.10), timetable version 1714, 1,000 runs per query:

| Query | Median (ms) | p95 (ms) |
|---|---:|---:|
| `find_stops("king william")` | 3.04 | 3.79 |
| `find_stops` with `near_stop_id` (busiest stop, 400 m) | 1.18 | 1.46 |
| `routes_at_stop` (busiest stop) | 2.00 | 2.53 |
| `next_departures` (busiest stop, weekday 08:00) | 0.71 | 0.83 |
| `next_departures` (random stop and time) | 0.71 | 1.00 |
| `next_departures` with `to_stop_id` (busiest stop, weekday 08:00) | 1.41 | 1.95 |
| `trip_stops` (longest trip, all 177 stops) | 0.76 | 0.99 |
| `next_departures` through the MCP client, end to end | 2.39 | 3.30 |

Full database build from the 15 MB zip: 9.0 s.

## Engineering decisions

- **Trips that run past midnight.** GTFS writes a trip that leaves at 00:50 on Monday's timetable as `24:50:00`. Times are stored as seconds since the start of the service day, and `next_departures` checks yesterday's, today's and tomorrow's services. Without that, late-night trips disappear after midnight. The real feed has departures as late as `29:40:00`.
- **Daylight saving.** All times use `Australia/Adelaide` (ACST +9:30 / ACDT +10:30). GTFS counts times from "noon minus 12 hours", so on the day the clocks change, a trip at 08:00 still means 08:00 on the clock. The `tzdata` package is a dependency because Windows has no timezone database for Python's `zoneinfo`. CI runs on Windows to catch this.
- **Which trips run on a date.** `calendar.txt` gives weekdays and a date range, and `calendar_dates.txt` adds or removes services on specific dates (e.g. public holidays). Both are applied.
- **Only real departures.** Stops where you can't board (`pickup_type = 1`, 4,378 in the feed) and the last stop of each trip are excluded. Parent stations, which group platforms, are rejected with a message pointing to a platform.
- **A to B as an option, not a new tool.** `next_departures` takes an optional `to_stop_id` instead of adding a new tool: fewer tools with clear jobs make it easier for the model to pick the right one. It keeps trips that reach the destination later and let you off there (`drop_off_type != 1`, 3,607 pickup-only stops in the feed). For loop routes that visit a stop twice (552 trips), it uses the first arrival after you board. If nothing ever goes directly, the tool says so (wrong side of the road, or a transfer is needed), so Claude doesn't tell the user to wait. Direct trips only: this is not a journey planner.
- **A separate tool for "where does this bus go?".** `to_stop_id` only checks a destination Claude already knows. In a real test ("Sefton Park to Hindley St"), Claude guessed six city stops one by one and none were on the route: the buses run down King William St to Victoria Sq. `trip_stops` lists a trip's stops in `stop_sequence` order, so one call shows the stop to get off at. It works per trip, not per route, because one route has several stopping patterns (express variants, short runs), and the trip being caught gives the exact stops and times. `next_departures` prints each trip id so Claude can pass it on. It uses the existing `stop_times(trip_id)` index, so no schema change was needed.
- **Fewer model round trips.** The database answers in about 1-3 ms; the slow part of a conversation is the model taking turns between tool calls. Early tests needed 7-10 calls per question, mostly searching: the stop on the other side of the road had a different suburb in its address (so text search missed it), and Claude had to call `next_departures` on every stop to see which way it went. Three changes target this:
  - `find_stops(near_stop_id=...)` finds stops within a radius (default 400 m), using a cheap latitude/longitude box in SQL and then exact haversine distances in Python for the few candidates. Distances are straight-line between stop coordinates from `stops.txt`, not walking routes, and the tool description says so, so Claude doesn't present them as walking times. This also fixed a wrong answer: without distances, Claude picked R1 North Tce for the University of Adelaide by its address; the near search shows B1 Pulteney St is closer (122 m vs 264 m from the University tram stop).
  - Each stop shows `towards ...`: the two most common headsigns of trips departing from it. That's too slow per request (up to 80 ms for a busy stop), so the loader precomputes a `stop_headsigns` table at build time (+1.7 s per build, 28k rows).
  - `trip_stops` lists 25 stops by default and always names the final stop, instead of 70+ lines for a cross-city route.
- **Schema versioning.** The database stores a schema version. If a newer release changes the tables, the loader rebuilds even when the timetable hasn't changed, and the server asks for a rebuild instead of failing on a missing column.
- **Fast reads.** The 15 MB zip becomes a 55 MB SQLite database with an index on `(stop_id, departure_secs)`, so a departures lookup is a short index range scan.
- **Safe updates.** The loader skips the download if `version.txt` is unchanged. It builds the database in a temporary file and swaps it in atomically, so a failed download never leaves a half-built database.
- **Read-only and validated.** The server opens SQLite in read-only mode, uses parameterised SQL only, and validates every input. Expected errors (unknown stop, bad input, missing data) become clear messages for Claude; unexpected errors are not exposed.
- **Thin protocol layer.** `server.py` only formats text; queries live in `db.py` and are tested with plain pytest. The current time is passed in as a parameter, so time-dependent tests are deterministic.
- **stdio transport.** stdout carries the MCP protocol, so the server never prints. Logs go to stderr.

## Project layout

```
adelaide_metro_mcp/
  gtfs_loader.py   download the GTFS zip and build the SQLite database
  db.py            read-only queries and input validation
  server.py        MCP tool definitions (thin formatting layer)
benchmarks/
  bench_queries.py query timings on the real database
tests/
  fixtures/mini_gtfs/  small made-up GTFS feed with the real column layout
```

## Roadmap

- Journeys with a transfer, and "stops near here" using the stop coordinates (so Claude can pick the closest stop to a street).
- Live delays in `next_departures` from the GTFS-Realtime trip updates feed (cached for its 60 s refresh interval).
- `service_disruptions` tool from the GTFS-Realtime service alerts feed.

## Data and attribution

Timetable data: **Adelaide Metro – Department for Infrastructure and Transport, South Australia**, from the [Adelaide Metro GTFS feeds](https://gtfs.adelaidemetro.com.au/). The data isn't included in this repository; the loader downloads it.

This is an independent project and is not affiliated with Adelaide Metro.

## License

MIT, see [LICENSE](LICENSE).
