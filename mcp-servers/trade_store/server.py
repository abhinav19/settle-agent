"""trade-store MCP server (read-only)."""

from mcp.server.mcpserver import MCPServer

from _shared.errors import tool_errors
from _shared.models import Dataset
from _shared.readonly import READ_ONLY
from _shared.serve import serve
from _shared.store import load_dataset
from trade_store.service import CounterpartyInstructionInfo, TradeInfo, TradeStore


def build_server(dataset: Dataset) -> MCPServer:
    svc = TradeStore(dataset)
    server = MCPServer("trade-store")

    @server.tool(annotations=READ_ONLY)
    def get_trade(trade_id: str) -> TradeInfo:
        """Trade economics, instruction/cut-off times and available securities and cash."""
        with tool_errors():
            return svc.get_trade(trade_id)

    @server.tool(annotations=READ_ONLY)
    def get_counterparty_instruction(trade_id: str) -> CounterpartyInstructionInfo:
        """The counterparty's matching instruction: MATCHED, MISSING or MISMATCH (with fields)."""
        with tool_errors():
            return svc.get_counterparty_instruction(trade_id)

    return server


def main() -> None:
    serve(build_server(load_dataset()), 8104)


if __name__ == "__main__":
    main()
