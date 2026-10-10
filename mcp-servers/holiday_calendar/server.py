"""holiday-calendar MCP server (read-only)."""

from mcp.server.mcpserver import MCPServer

from _shared.errors import tool_errors
from _shared.models import Dataset
from _shared.readonly import READ_ONLY
from _shared.serve import serve
from _shared.store import load_dataset
from holiday_calendar.service import BusinessDay, HolidayCalendar, NextBusinessDay


def build_server(dataset: Dataset) -> MCPServer:
    svc = HolidayCalendar(dataset)
    server = MCPServer("holiday-calendar")

    @server.tool(annotations=READ_ONLY)
    def is_business_day(market: str, date: str) -> BusinessDay:
        """Whether an ISO date (YYYY-MM-DD) is a business day on a market (MIC code)."""
        with tool_errors():
            return svc.is_business_day(market, date)

    @server.tool(annotations=READ_ONLY)
    def next_business_day(market: str, date: str) -> NextBusinessDay:
        """The first business day strictly after an ISO date on a market (MIC code)."""
        with tool_errors():
            return svc.next_business_day(market, date)

    return server


def main() -> None:
    serve(build_server(load_dataset()), 8103)


if __name__ == "__main__":
    main()
