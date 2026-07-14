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

            print("\n-- list_payment_terms --")
            r = await session.call_tool("list_payment_terms", {"query": "30"})
            print(json.dumps(_content(r), indent=2, default=str))

            # full credit-sale flow: make an order, then bill it on 30-day terms
            print("\n-- record_sale (for invoicing) --")
            r = await session.call_tool("record_sale",
                                        {"customer": "MCP Invoice Customer", "product": "Scaffold Widget",
                                         "qty": 4, "confirm": True})
            sale = _content(r)
            print(json.dumps(sale, indent=2, default=str))
            order_name = sale.get("name")

            print("\n-- post_customer_invoice (30 Days credit) --")
            r = await session.call_tool("post_customer_invoice",
                                        {"order": order_name, "payment_term": "30 Days"})
            inv = _content(r)
            print(json.dumps(inv, indent=2, default=str))
            assert inv.get("_ok"), f"invoice not ok: {inv}"
            m = inv["invoices"][0]
            assert m["state"] == "posted", m
            assert m["invoice_date_due"], f"no credit due date set: {m}"
            print(f"\nPASS: {m['name']} posted, total {m['amount_total']}, due {m['invoice_date_due']}")

            # ---- FEFO delivery: two dated lots, sell within the earliest one ----
            PROD = "MCP FEFO Widget"
            print("\n-- stock_in_lot (two lots, different expiry) --")
            r = await session.call_tool("stock_in_lot",
                                        {"product": PROD, "lot": "FEFO-EARLY", "qty": 5,
                                         "expiration_date": "2026-09-01"})
            print(json.dumps(_content(r), indent=2, default=str))
            r = await session.call_tool("stock_in_lot",
                                        {"product": PROD, "lot": "FEFO-LATE", "qty": 5,
                                         "expiration_date": "2027-06-01"})
            print(json.dumps(_content(r), indent=2, default=str))

            print("\n-- record_sale (storable FEFO product) --")
            r = await session.call_tool("record_sale",
                                        {"customer": "MCP Invoice Customer", "product": PROD,
                                         "qty": 3, "confirm": True})
            sale = _content(r)
            print(json.dumps(sale, indent=2, default=str))

            print("\n-- deliver_sale (FEFO reserve + validate) --")
            r = await session.call_tool("deliver_sale", {"order": sale["name"]})
            deliv = _content(r)
            print(json.dumps(deliv, indent=2, default=str))
            assert deliv.get("_ok"), f"delivery not ok: {deliv}"
            pk = deliv["pickings"][0]
            assert pk["state"] == "done", pk
            picked = {mv["lot"] for mv in pk["moves"]}
            assert picked == {"FEFO-EARLY"}, f"FEFO should pick earliest-expiry lot, got {picked}"
            print(f"\nPASS: {pk['name']} done, FEFO picked {picked} "
                  f"(earliest expiry), qty {[mv['qty'] for mv in pk['moves']]}")


if __name__ == "__main__":
    asyncio.run(main())
