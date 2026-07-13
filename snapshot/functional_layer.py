"""
The functional layer  --  the part that actually matters.

This is the "semantic / intent-level" API an AI agent should drive, instead of
raw create/write on sale.order & account.move. Each method encapsulates the
CORRECT Odoo flow so the agent only supplies business intent (who, what, how
many) and can't produce a broken order.

WHY this layer exists (the onchange problem):
  In Odoo 17+/19, the price-sensitive fields on order lines -- price_unit,
  tax_id, name (description), product_uom, and the header's pricelist / fiscal
  position / payment terms -- are *computed, stored* fields that depend on
  product_id / partner_id. So a well-formed create() that supplies the trigger
  fields (partner_id, product_id, qty) makes the server compute prices & taxes
  for you. The job of this layer is to:
    1. supply exactly those trigger fields (nothing hand-priced),
    2. let the server compute,
    3. READ BACK totals and verify they're non-zero / sane before returning,
    4. optionally run the real workflow method (action_confirm) rather than
       poking the state field.
  For the rare field that needs an explicit onchange, call client.execute_kw(
  model, "onchange", ...) -- but for standard sales/purchase in v19 the
  compute-on-create path above is the robust one.
"""


class FunctionalAgent:
    def __init__(self, client):
        self.c = client

    # --- resolvers (idempotent lookups so the agent works by name) -----------
    def find_or_create_partner(self, name, is_company=True):
        found = self.c.search("res.partner", [("name", "=", name)], limit=1)
        if found:
            return found[0]
        return self.c.create("res.partner", {"name": name, "is_company": is_company})

    def find_or_create_product(self, name, list_price=0.0, standard_price=0.0):
        found = self.c.search("product.product", [("name", "=", name)], limit=1)
        if found:
            return found[0]
        return self.c.create("product.product", {
            "name": name,
            "list_price": list_price,      # sales price
            "standard_price": standard_price,  # cost (purchase)
            "type": "consu",               # storable needs stock module; consu is safe
            "purchase_ok": True,
            "sale_ok": True,
        })

    # --- SALES ---------------------------------------------------------------
    def record_sale(self, customer, lines, confirm=False):
        """
        customer : partner name (str) or id (int)
        lines    : list of (product_name_or_id, qty)
        confirm  : if True, run action_confirm (the real workflow, not a state poke)
        """
        partner_id = customer if isinstance(customer, int) else self.find_or_create_partner(customer)

        order_lines = []
        for prod, qty in lines:
            pid = prod if isinstance(prod, int) else self.find_or_create_product(prod)
            # Only trigger fields. price_unit / tax_id / name compute server-side.
            order_lines.append((0, 0, {"product_id": pid, "product_uom_qty": qty}))

        so_id = self.c.create("sale.order", {
            "partner_id": partner_id,
            "order_line": order_lines,
        })

        if confirm:
            self.c.call("sale.order", "action_confirm", [so_id])

        return self._summarize_sale(so_id)

    def _summarize_sale(self, so_id):
        rec = self.c.read("sale.order", [so_id],
                          ["name", "state", "amount_untaxed", "amount_tax", "amount_total"])[0]
        line_recs = self.c.search_read(
            "sale.order.line", [("order_id", "=", so_id)],
            ["product_id", "product_uom_qty", "price_unit", "price_subtotal", "tax_ids"])
        rec["lines"] = line_recs
        # verification guard: a real priced order should not total zero
        rec["_ok"] = rec["amount_total"] > 0
        return rec

    # --- PURCHASE ------------------------------------------------------------
    def record_purchase(self, vendor, lines, confirm=False):
        """
        vendor  : partner name (str) or id (int)
        lines   : list of (product_name_or_id, qty[, price_unit])
                  price_unit optional -- if omitted, Odoo computes it from the
                  product's cost / vendor pricelist.
        confirm : if True, run button_confirm (draft -> purchase order)
        """
        partner_id = vendor if isinstance(vendor, int) else self.find_or_create_partner(vendor)

        order_lines = []
        for line in lines:
            prod, qty = line[0], line[1]
            pid = prod if isinstance(prod, int) else self.find_or_create_product(prod)
            vals = {"product_id": pid, "product_qty": qty}
            if len(line) > 2 and line[2] is not None:
                vals["price_unit"] = line[2]  # explicit override; else computed
            order_lines.append((0, 0, vals))

        po_id = self.c.create("purchase.order", {
            "partner_id": partner_id,
            "order_line": order_lines,
        })

        if confirm:
            self.c.call("purchase.order", "button_confirm", [po_id])

        return self._summarize_purchase(po_id)

    def _summarize_purchase(self, po_id):
        rec = self.c.read("purchase.order", [po_id],
                          ["name", "state", "amount_untaxed", "amount_tax", "amount_total"])[0]
        line_recs = self.c.search_read(
            "purchase.order.line", [("order_id", "=", po_id)],
            ["product_id", "product_qty", "price_unit", "price_subtotal"])
        rec["lines"] = line_recs
        rec["_ok"] = rec["amount_total"] > 0
        return rec
