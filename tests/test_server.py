"""Server-level tests: connect a real MCP client to the server in-process.

No network and no subprocess: the SDK's Client accepts an MCPServer instance
directly, so these tests exercise the full MCP request/response path quickly.
"""

import asyncio

import pytest

from mcp import Client

from adelaide_metro_mcp.server import mcp


def call(tool: str, args: dict):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def list_tools():
    async def go():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools

    return asyncio.run(go())


def text(result) -> str:
    return result.content[0].text


def test_tools_are_listed_and_read_only():
    tools = {t.name: t for t in list_tools()}
    assert set(tools) == {"find_stops", "routes_at_stop", "next_departures", "timetable_info"}
    for t in tools.values():
        assert t.description, f"{t.name} needs a description"
        assert t.annotations.read_only_hint is True


def test_limit_bounds_are_in_the_schema():
    find = next(t for t in list_tools() if t.name == "find_stops")
    limit = find.input_schema["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"]) == (1, 20)


def test_find_stops_returns_readable_text(server_data):
    result = call("find_stops", {"query": "central"})
    assert not result.is_error
    assert text(result) == (
        "Found 1 stop(s) for 'central':\n"
        "- Central Station (stop 10001, id S1) - 1 Test Rd, Testville"
    )


def test_find_stops_no_match_message(server_data):
    result = call("find_stops", {"query": "nowhere"})
    assert not result.is_error
    assert "No stops found" in text(result)


def test_find_stops_bad_query_is_a_clear_error(server_data):
    result = call("find_stops", {"query": "a"})
    assert result.is_error
    assert "at least 2 characters" in text(result)


@pytest.mark.parametrize("limit", [0, 21])
def test_find_stops_limit_out_of_range_is_rejected(server_data, limit):
    result = call("find_stops", {"query": "central", "limit": limit})
    assert result.is_error


def test_routes_at_stop_returns_routes(server_data):
    result = call("routes_at_stop", {"stop_id": "S1"})
    assert not result.is_error
    assert text(result) == (
        "Routes at Central Station (stop 10001, id S1) - 1 Test Rd, Testville:\n"
        "- 100 (Bus): Central Station to Beach Interchange\n"
        "- T1 (Tram): Central Station to Beach (tram)"
    )


def test_routes_at_unknown_stop_is_a_clear_error(server_data):
    result = call("routes_at_stop", {"stop_id": "NOPE"})
    assert result.is_error
    assert "No stop with id 'NOPE'" in text(result)


def test_missing_database_tells_user_how_to_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("ADELAIDE_METRO_DATA_DIR", str(tmp_path))
    result = call("find_stops", {"query": "central"})
    assert result.is_error
    assert "python -m adelaide_metro_mcp.gtfs_loader" in text(result)


# --- next_departures and timetable_info -------------------------------------------------

from datetime import datetime

from adelaide_metro_mcp import server
from adelaide_metro_mcp.db import ADELAIDE_TZ


@pytest.fixture
def clock(monkeypatch):
    """Set the server's 'now' to a fixed Adelaide time: clock('2026-11-09T07:00')."""
    def set_time(iso: str):
        fixed = datetime.fromisoformat(iso).replace(tzinfo=ADELAIDE_TZ)
        monkeypatch.setattr(server, "_now", lambda: fixed)
    return set_time


def test_next_departures_returns_readable_text(server_data, clock):
    clock("2026-11-09T07:00")  # a Monday
    result = call("next_departures", {"stop_id": "S1", "limit": 2})
    assert not result.is_error
    assert text(result) == (
        "Scheduled departures from Central Station (stop 10001, id S1) - 1 Test Rd, Testville\n"
        "As of Mon 09 Nov 07:00 (timetable only, no live delays):\n"
        "- 07:30  Bus 100 to Beach Interchange (in 30 min)\n"
        "- 08:00  Bus 100 to Beach Interchange (in 1 h)"
    )


def test_next_departures_after_midnight_shows_tomorrow(server_data, clock):
    clock("2026-11-13T23:30")  # Friday night
    result = call("next_departures", {"stop_id": "S1"})
    assert text(result).endswith("- 00:50 tomorrow  Tram T1 to Beach (in 1 h 20 min)")


def test_next_departures_route_filter(server_data, clock):
    clock("2026-11-13T23:30")
    result = call("next_departures", {"stop_id": "S1", "route": "100"})
    assert text(result) == (
        "No scheduled departures for route 100 from Central Station (stop 10001, id S1) "
        "- 1 Test Rd, Testville in the next 2 hours."
    )


def test_next_departures_none_message(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("next_departures", {"stop_id": "S3", "minutes_ahead": 30})
    assert not result.is_error
    assert "No scheduled departures from Beach Interchange" in text(result)
    assert "in the next 30 minutes" in text(result)


def test_next_departures_unknown_route_is_a_clear_error(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("next_departures", {"stop_id": "S1", "route": "999"})
    assert result.is_error
    assert "No route '999'" in text(result)


@pytest.mark.parametrize("minutes", [0, 1441])
def test_next_departures_minutes_out_of_range_is_rejected(server_data, minutes):
    result = call("next_departures", {"stop_id": "S1", "minutes_ahead": minutes})
    assert result.is_error


def test_timetable_info_text(server_data, clock):
    clock("2026-11-09T07:00")
    result = call("timetable_info", {})
    assert not result.is_error
    body = text(result)
    assert "Timetable version 999" in body
    assert "Valid from 2026-01-01 to 2027-12-31" in body
    assert "3 stops, 2 routes, 5 trips" in body
    assert server.ATTRIBUTION in body
    assert "Warning" not in body


def test_timetable_info_warns_when_expired(server_data, clock):
    clock("2028-01-10T07:00")
    assert "this timetable has expired" in text(call("timetable_info", {}))
