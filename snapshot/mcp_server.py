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
# CAPABILITY: customer invoice  (post GST invoice + credit terms)  (added 2026-07-14)
# ============================================================================
@mcp.tool()
def post_customer_invoice(order: str, payment_term: str | None = None,
                          auto_confirm: bool = True) -> dict:
    """Create and POST a customer invoice for an existing sale order.

    - order: the sale order name (e.g. 'S00007') or id.
    - payment_term: optional credit term name (e.g. '30 Days', 'Immediate Payment');
      it sets the invoice's due date (the receivable). Omit for the order/partner default.
    - auto_confirm: confirm the quotation first if it is still a draft (invoicing
      requires a confirmed order).

    Taxes (including GST CGST/SGST/IGST wherever configured) flow from the order
    lines and are returned split by component in `taxes`. Returns the invoice number,
    state (should be 'posted'), totals, tax breakdown, and the credit due date
    (invoice_date_due). Use record_sale first to create the order, then this to bill it."""
    return _safe(lambda: agent().invoice_sale(order, payment_term=payment_term,
                                              auto_confirm=auto_confirm))


# ============================================================================
# CAPABILITY: inventory  (seed/receive lot stock with expiry, for FEFO)  (added 2026-07-14)
# ============================================================================
@mcp.tool()
def stock_in_lot(product: str, lot: str, qty: float,
                 expiration_date: str | None = None) -> dict:
    """Put on-hand stock of a LOT-tracked product in (opening/receipt via inventory
    adjustment), creating the batch/lot with an optional expiry date.

    - product: product name; it is auto-configured as storable + lot-tracked in the
      FEFO category (so deliveries pick earliest-expiry first). Reused if it exists.
    - lot: the batch/lot number (e.g. 'CONF-2406').
    - qty: quantity to place on hand.
    - expiration_date: 'YYYY-MM-DD' (drives FEFO ordering). Optional but recommended
      for perishable agro-inputs.

    Call this BEFORE record_sale/deliver_sale so there is stock (and a dated lot) to
    ship. Returns the resulting on-hand quant with its computed removal date."""
    return _safe(lambda: agent().add_lot_stock(product, lot, qty,
                                               expiration_date=expiration_date))


# ============================================================================
# CAPABILITY: stock delivery  (reserve FEFO lot + validate picking)  (added 2026-07-14)
# ============================================================================
@mcp.tool()
def deliver_sale(order: str, auto_confirm: bool = True) -> dict:
    """Reserve stock and VALIDATE the delivery for a sale order, picking the
    earliest-expiry lot (FEFO), then read back which lot(s) shipped.

    - order: the sale order name (e.g. 'S00007') or id.
    - auto_confirm: confirm the quotation first if still a draft (confirming a sale
      of a storable product is what creates the delivery picking).

    Requires the product to be storable + lot-tracked with stock on hand (use
    stock_in_lot first). Returns per-picking state (should be 'done') and, per move,
    the picked lot and its expiration date so you can verify the correct batch left
    stock. Typical chain: stock_in_lot -> record_sale -> deliver_sale -> post_customer_invoice."""
    return _safe(lambda: agent().deliver_sale(order, auto_confirm=auto_confirm))


# ============================================================================
# CAPABILITY: product master  (HSN + GST split + lot/expiry + UoM/pack)  (added 2026-07-14)
# ============================================================================
@mcp.tool()
def create_product(name: str, hsn: str | None = None, gst_rate: float | None = None,
                   uom: str = "Units", tracking: str | None = None,
                   use_expiration: bool = False, list_price: float | None = None,
                   standard_price: float | None = None, category: str | None = None,
                   pack_name: str | None = None, pack_size: float | None = None,
                   interstate_gst: bool = False) -> dict:
    """Create (or update, idempotent by name) a product master (product.template).

    - name: product name (e.g. 'Amida 17.8% SL 250ml').
    - hsn: HSN/SAC code (e.g. '3808'); stored in the l10n_in HSN field.
    - gst_rate: GST percent (e.g. 18). Resolves BOTH a sales and a purchase tax:
      intra-state -> a group tax that splits into CGST + SGST (each rate/2);
      set interstate_gst=True for a single IGST tax instead. Omit to leave taxes unset.
    - uom: base unit of measure name (default 'Units').
    - tracking: 'lot', 'serial', or 'none'. 'lot'/'serial' make the product storable.
    - use_expiration: enable expiry dates (perishable agro-inputs); with tracking='lot'
      the product is filed in the FEFO category so deliveries ship earliest-expiry first.
    - list_price / standard_price: sales price / cost (omit to leave the Odoo default).
    - category: product.category name (defaults to the FEFO category for lot+expiry).
    - pack_name + pack_size: optional pack/case UoM, e.g. ('Case of 10', 10).

    Reads the record back and returns id, name, HSN, uom, tracking/expiry, category,
    and the sales/purchase taxes with their CGST/SGST components. _ok is True only when
    the requested HSN, tax, and tracking actually landed on the record."""
    pack = {"name": pack_name, "contains": pack_size} if (pack_name and pack_size) else None
    return _safe(lambda: agent().create_product(
        name, hsn=hsn, gst_rate=gst_rate, uom=uom, tracking=tracking,
        use_expiration=use_expiration, list_price=list_price, standard_price=standard_price,
        category=category, pack=pack, interstate_gst=interstate_gst))


# ============================================================================
# CAPABILITY: lookups (read-only helpers so the agent can resolve names)
# ============================================================================
@mcp.tool()
def list_payment_terms(query: str = "") -> dict:
    """List available payment (credit) terms, e.g. 'Immediate Payment', '30 Days'.
    Read-only. Use before post_customer_invoice to pick a valid term name."""
    def go():
        domain = [("name", "ilike", query)] if query else []
        rows = agent().c.search_read("account.payment.term", domain, ["id", "name"], limit=25)
        return {"ok": True, "count": len(rows), "payment_terms": rows}
    return _safe(go)
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
