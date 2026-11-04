"""MCP tool definitions.

This module is kept thin: each tool validates its input, calls a query in
db.py, and formats a short text answer. Data access lives in db.py so it can
be tested without an MCP client.

Note: with the stdio transport, stdout carries the MCP protocol messages, so
nothing in this server may print() to stdout. Use logging (stderr) instead.
"""

from __future__ import annotations

from contextlib import closing
from typing import Annotated

from pydantic import Field

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from adelaide_metro_mcp import db

ATTRIBUTION = "Adelaide Metro – Department for Infrastructure and Transport, South Australia"

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

mcp = MCPServer(
    name="adelaide-metro",
    instructions=(
        "Answers questions about Adelaide Metro buses, trains and trams using official "
        "GTFS timetable data. To get departures, first call find_stops to get a stop_id. "
        f"Data source: {ATTRIBUTION}."
    ),
)

Limit = Annotated[int, Field(ge=1, le=db.MAX_LIMIT, description=f"Maximum results (1-{db.MAX_LIMIT}).")]


def _run(query_fn):
    """Open the database, run a query, and turn expected failures into ToolErrors.

    ToolError messages are shown to the client as-is. Any other exception is
    treated by the SDK as a crash and its message is hidden, which is what we
    want for genuine bugs.
    """
    try:
        with closing(db.connect()) as conn:
            return query_fn(conn)
    except (ValueError, db.NotFoundError, db.DatabaseNotReadyError) as exc:
        raise ToolError(str(exc)) from exc


def _format_stop(s: db.Stop) -> str:
    code = f"stop {s.stop_code}, " if s.stop_code else ""
    desc = f" - {s.stop_desc}" if s.stop_desc else ""
    return f"{s.stop_name} ({code}id {s.stop_id}){desc}"


@mcp.tool(annotations=READ_ONLY)
def find_stops(
    query: Annotated[str, Field(description="Stop name, street, suburb or stop number, e.g. 'Glenelg' or 'King William D2'.")],
    limit: Limit = 10,
) -> str:
    """Search Adelaide Metro stops by name, street, suburb or the stop number shown on the sign.

    Returns each stop's name, stop number, id and address. Use the id with
    next_departures or routes_at_stop.
    """
    stops = _run(lambda conn: db.find_stops(conn, query, limit))
    if not stops:
        return f"No stops found for {query.strip()!r}. Try a shorter name or a street name."
    lines = [f"Found {len(stops)} stop(s) for {query.strip()!r}:"]
    lines += [f"- {_format_stop(s)}" for s in stops]
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def routes_at_stop(
    stop_id: Annotated[str, Field(description="Stop id from find_stops.")],
) -> str:
    """List the bus, train and tram routes that stop at a given stop."""
    stop, routes = _run(lambda conn: db.routes_at_stop(conn, stop_id))
    if not routes:
        return f"No scheduled routes found at {_format_stop(stop)}."
    lines = [f"Routes at {_format_stop(stop)}:"]
    lines += [f"- {r.short_name} ({r.mode}): {r.long_name}" for r in routes]
    return "\n".join(lines)


def main() -> None:
    """Entry point: run the server over stdio."""
    mcp.run()  # transport defaults to "stdio"


if __name__ == "__main__":
    main()
