"""security-master MCP server (read-only)."""

from mcp.server.mcpserver import MCPServer

from _shared.errors import tool_errors
from _shared.models import Dataset
from _shared.readonly import READ_ONLY
from _shared.serve import serve
from _shared.store import load_dataset
from security_master.service import InstrumentInfo, SecurityMaster


def build_server(dataset: Dataset) -> MCPServer:
    svc = SecurityMaster(dataset)
    server = MCPServer("security-master")

    @server.tool(annotations=READ_ONLY)
    def get_instrument(isin: str) -> InstrumentInfo:
        """Static data for an instrument: market, currency, lot size and status."""
        with tool_errors():
            return svc.get_instrument(isin)

    return server


def main() -> None:
    serve(build_server(load_dataset()), 8101)


if __name__ == "__main__":
    main()
