"""Run an MCP server over stdio (default) or stateless streamable HTTP.

Environment: ``SETTLE_MCP_TRANSPORT`` (stdio | streamable-http), ``SETTLE_MCP_HOST``
(default 127.0.0.1), ``SETTLE_MCP_PORT`` (default per server). HTTP mode is stateless, so the
agent can connect per call and servers can restart freely.
"""

import os

from mcp.server.mcpserver import MCPServer

TRANSPORT_ENV = "SETTLE_MCP_TRANSPORT"
HOST_ENV = "SETTLE_MCP_HOST"
PORT_ENV = "SETTLE_MCP_PORT"


def serve(server: MCPServer, default_port: int) -> None:
    transport = os.environ.get(TRANSPORT_ENV, "stdio")
    if transport == "stdio":
        server.run("stdio")
    elif transport == "streamable-http":
        server.run(
            "streamable-http",
            host=os.environ.get(HOST_ENV, "127.0.0.1"),
            port=int(os.environ.get(PORT_ENV, default_port)),
            stateless_http=True,
        )
    else:
        raise SystemExit(f"unsupported {TRANSPORT_ENV}={transport!r}")
