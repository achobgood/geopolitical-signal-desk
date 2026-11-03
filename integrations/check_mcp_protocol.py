"""Run separately using .venv-mcp/bin/python integrations/check_mcp_protocol.py."""
import asyncio
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(command=sys.executable, args=["integrations/mcp_server.py"], env={})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert len(tools) == 8
            by_name = {t.name: t for t in tools}
            assert by_name["list_leads"].annotations.readOnlyHint is True
            assert by_name["ingest_lead"].annotations.readOnlyHint is False
            assert by_name["collect_official_feed"].annotations.openWorldHint is True
            denied = await session.call_tool("readiness", {})
            assert denied.isError is True
            print("MCP initialize, discovery (8 tools), annotations and missing-credential rejection passed")


if __name__ == "__main__":
    asyncio.run(main())
