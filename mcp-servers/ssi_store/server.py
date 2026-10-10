"""ssi-store MCP server (read-only)."""

from mcp.server.mcpserver import MCPServer

from _shared.errors import tool_errors
from _shared.models import Dataset
from _shared.readonly import READ_ONLY
from _shared.serve import serve
from _shared.store import load_dataset
from ssi_store.service import Ssi, SsiStore, TradeSsi


def build_server(dataset: Dataset) -> MCPServer:
    svc = SsiStore(dataset)
    server = MCPServer("ssi-store")

    @server.tool(annotations=READ_ONLY)
    def get_ssi(bic: str, currency: str) -> Ssi:
        """Current active standing settlement instruction for a counterparty BIC and currency."""
        with tool_errors():
            return svc.get_ssi(bic, currency)

    @server.tool(annotations=READ_ONLY)
    def get_ssi_for_trade(trade_id: str) -> TradeSsi:
        """The SSI a trade was instructed with, compared against the current active SSI."""
        with tool_errors():
            return svc.get_ssi_for_trade(trade_id)

    return server


def main() -> None:
    serve(build_server(load_dataset()), 8102)


if __name__ == "__main__":
    main()
