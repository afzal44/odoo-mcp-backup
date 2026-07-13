"""
Odoo Functional MCP server  --  the "steering wheel" over the functional layer.

This exposes our hand-written, correctness-guaranteed functions (functional_layer.py)
to any MCP client (Claude Desktop, Claude Code, Cursor, ...) as safe, named,
semantic tools. It does NOT expose raw CRUD.

HOW WE GROW THIS (the repeatable process Boss & Ops agreed on):
    1. CHECK  - confirm the Odoo models/flow for the new capability (dev first).
    2. BUILD  - add a method to functional_layer.py that does the correct flow.
    3. PLAN   - decide the tool's name, inputs, and read-back verification.
    4. TEST   - prove it works via test_mcp.py (real MCP call over stdio).
    5. WRAP   - add one @mcp.tool() block below, in its capability section.
Each capability lives in its own clearly-marked section. Adding "upload document"
tomorrow = a new section + a new functional_layer method. Nothing else changes.

Run (stdio, for local MCP clients):
    .venv/bin/python mcp_server.py
"""
import os

from mcp.server.fastmcp import FastMCP

from odoo_client import from_keyfile
from functional_layer import FunctionalAgent

KEYFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".mlrd_agent_key")

mcp = FastMCP("odoo-functional")

# lazy singleton so the server starts even if Odoo is briefly unreachable;
# the connection (and any auth error) surfaces on first tool call instead.
_agent = None


def agent():
    global _agent
    if _agent is None:
        client = from_keyfile(KEYFILE)
        client.login()
        _agent = FunctionalAgent(client)
    return _agent


def _safe(fn):
    """Return the result, or a structured error the LLM can read & react to."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - deliberately surface to the client
        return {"error": str(e), "ok": False}


# ============================================================================
# CAPABILITY: sales  (added 2026-07-13)
# ============================================================================
@mcp.tool()
def record_sale(customer: str, product: str, qty: float, confirm: bool = False) -> dict:
    """Record a sales order. Supply the customer name, product name, and quantity;
    Odoo computes price & taxes. Set confirm=True to confirm the order (draft->sale).
    Returns the order name, state, and server-computed totals."""
    return _safe(lambda: agent().record_sale(customer, [(product, qty)], confirm=confirm))


# ============================================================================
# CAPABILITY: purchase  (added 2026-07-13)
# ============================================================================
@mcp.tool()
def record_purchase(vendor: str, product: str, qty: float,
                    price: float | None = None, confirm: bool = False) -> dict:
    """Record a purchase order. Supply vendor, product, quantity; price is optional
    (omit to use the product's cost / vendor pricelist). Set confirm=True to confirm
    (draft->purchase). Returns order name, state, and server-computed totals."""
    return _safe(lambda: agent().record_purchase(vendor, [(product, qty, price)], confirm=confirm))


# ============================================================================
# CAPABILITY: lookups (read-only helpers so the agent can resolve names)
# ============================================================================
@mcp.tool()
def find_partner(name: str) -> dict:
    """Find contacts (customers/vendors) whose name matches. Read-only."""
    def go():
        rows = agent().c.search_read("res.partner", [("name", "ilike", name)],
                                     ["id", "name", "email", "phone"], limit=10)
        return {"ok": True, "count": len(rows), "partners": rows}
    return _safe(go)


@mcp.tool()
def list_products(query: str = "", limit: int = 10) -> dict:
    """List sellable/purchasable products, optionally filtered by name. Read-only."""
    def go():
        domain = [("name", "ilike", query)] if query else []
        rows = agent().c.search_read("product.product", domain,
                                     ["id", "name", "list_price", "standard_price"], limit=limit)
        return {"ok": True, "count": len(rows), "products": rows}
    return _safe(go)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
