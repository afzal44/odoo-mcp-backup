"""
Real MCP client test: spins up mcp_server.py over stdio, does the protocol
handshake, lists tools, and calls them against mlrd. This is the "TEST" step of
our Check->Build->Plan->Test->Wrap process. Run:

    .venv/bin/python test_mcp.py
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))


def _content(result):
    # tool results come back as content blocks; grab text and parse JSON if we can
    out = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            out.append(json.loads(text))
        except Exception:
            out.append(text)
    return out[0] if len(out) == 1 else out


async def main():
    params = StdioServerParameters(command=sys.executable, args=["mcp_server.py"], cwd=HERE)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])

            print("\n-- list_products --")
            r = await session.call_tool("list_products", {"query": "Scaffold"})
            print(json.dumps(_content(r), indent=2, default=str))

            print("\n-- record_sale (confirm) --")
            r = await session.call_tool("record_sale",
                                        {"customer": "MCP Test Customer", "product": "Scaffold Widget",
                                         "qty": 5, "confirm": True})
            print(json.dumps(_content(r), indent=2, default=str))

            print("\n-- record_purchase (confirm) --")
            r = await session.call_tool("record_purchase",
                                        {"vendor": "MCP Test Vendor", "product": "Scaffold Widget",
                                         "qty": 8, "confirm": True})
            print(json.dumps(_content(r), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
