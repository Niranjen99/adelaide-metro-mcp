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
    assert {"find_stops", "routes_at_stop"} <= tools.keys()
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
