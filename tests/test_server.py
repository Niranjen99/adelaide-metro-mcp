"""Server-level tests: connect a real MCP client to the server in-process.

No network and no subprocess: the SDK's Client accepts an MCPServer instance
directly, so these tests exercise the full MCP request/response path quickly.
"""

import asyncio

from mcp import Client

from adelaide_metro_mcp.server import mcp


def run(coro):
    return asyncio.run(coro)


def test_hello_tool_is_listed():
    async def go():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            return [t.name for t in tools.tools]

    assert "hello" in run(go())


def test_hello_tool_returns_greeting():
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool("hello", {})

    result = run(go())
    assert not result.is_error
    assert "Adelaide Metro" in result.content[0].text
