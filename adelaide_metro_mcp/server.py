"""MCP tool definitions.

This module is kept thin: each tool validates its input and formats a short
text answer. Data access lives in separate modules so it can be tested
without an MCP client.

Note: with the stdio transport, stdout carries the MCP protocol messages, so
nothing in this server may print() to stdout. Use logging (stderr) instead.
"""

from mcp.server import MCPServer

ATTRIBUTION = "Adelaide Metro – Department for Infrastructure and Transport, South Australia"

mcp = MCPServer(
    name="adelaide-metro",
    instructions=(
        "Answers questions about Adelaide Metro buses, trains and trams using "
        f"official GTFS data. Data: {ATTRIBUTION}."
    ),
)


@mcp.tool()
def hello() -> str:
    """Temporary health check: confirms the Adelaide Metro server is running."""
    return "Hello from the Adelaide Metro MCP server."


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run()  # transport defaults to "stdio"


if __name__ == "__main__":
    main()
