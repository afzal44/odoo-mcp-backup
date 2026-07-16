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

    # --- SALES INVOICE  (credit terms + GST-aware) --------------------------
    def find_payment_term(self, term):
        """Resolve an account.payment.term by id or name. Returns id or None.
        Falls back to a contains match so '30' finds '30 Days'."""
        if isinstance(term, int):
            return term
        found = self.c.search("account.payment.term", [("name", "=", term)], limit=1)
        if not found:
            found = self.c.search("account.payment.term", [("name", "ilike", term)], limit=1)
        return found[0] if found else None

    def _resolve_sale_order(self, order):
        if isinstance(order, int):
            return order
        found = self.c.search("sale.order", [("name", "=", order)], limit=1)
        if not found:
            raise ValueError(f"Sale order not found: {order!r}")
        return found[0]

    def invoice_sale(self, order, payment_term=None, auto_confirm=True):
        """
        Create AND post a customer invoice for an existing sale order, applying an
        optional payment term (credit period) so the receivable gets a due date.

        order        : sale.order name (e.g. 'S00007') or id.
        payment_term : account.payment.term name/id (e.g. '30 Days'); optional.
                       Sets the credit due date (invoice_date_due) on the invoice.
        auto_confirm : confirm the SO first if it's still a draft/quotation
                       (invoicing requires a confirmed order).

        WHY the wizard (not _create_invoices): sale.order._create_invoices is a
        PRIVATE method (leading underscore) and Odoo refuses to call it over
        JSON-RPC. The public, UI-equivalent path is the sale.advance.payment.inv
        wizard with the sale order in the context's active_ids.

        Taxes are NOT hand-set: the invoice inherits the SO lines' computed taxes,
        so wherever GST (CGST/SGST/IGST) is configured it flows through and is
        reported back in `taxes`, split by component.
        """
        so_id = self._resolve_sale_order(order)
        rec = self.c.read("sale.order", [so_id],
                          ["name", "state", "invoice_status", "invoice_ids"])[0]

        # 1. ensure confirmed (draft/sent orders can't be invoiced)
        if rec["state"] in ("draft", "sent"):
            if not auto_confirm:
                return {"_ok": False,
                        "error": f"Order {rec['name']} is '{rec['state']}'; "
                                 f"confirm it first or pass auto_confirm=True."}
            self.c.call("sale.order", "action_confirm", [so_id])

        # 2. apply the credit term BEFORE invoicing so it flows onto the invoice
        if payment_term is not None:
            term_id = self.find_payment_term(payment_term)
            if term_id is None:
                return {"_ok": False, "error": f"Payment term not found: {payment_term!r}"}
            self.c.write("sale.order", [so_id], {"payment_term_id": term_id})

        # 3. anything to invoice?
        rec = self.c.read("sale.order", [so_id], ["name", "invoice_status", "invoice_ids"])[0]
        before_inv = set(rec["invoice_ids"])
        if rec["invoice_status"] != "to invoice":
            if before_inv:
                return {"_ok": True, "note": f"Order {rec['name']} already invoiced.",
                        "order": rec["name"], "invoices": self._summarize_invoices(list(before_inv))}
            return {"_ok": False,
                    "error": f"Order {rec['name']} has invoice_status="
                             f"'{rec['invoice_status']}'; nothing to invoice."}

        # 4. public wizard flow -> create the draft invoice(s)
        ctx = {"active_model": "sale.order", "active_ids": [so_id], "active_id": so_id}
        wiz_id = self.c.execute_kw("sale.advance.payment.inv", "create",
                                   [{"advance_payment_method": "delivered"}], {"context": ctx})
        self.c.execute_kw("sale.advance.payment.inv", "create_invoices", [[wiz_id]], {"context": ctx})

        # 5. post the newly-created invoice(s) via the real workflow method
        rec = self.c.read("sale.order", [so_id], ["name", "invoice_ids"])[0]
        new_inv = [i for i in rec["invoice_ids"] if i not in before_inv]
        if not new_inv:
            return {"_ok": False, "error": "Invoice creation returned no new invoice."}
        self.c.call("account.move", "action_post", new_inv)

        # 6. read back + verify (posted, non-zero, and a due date actually landed)
        summary = self._summarize_invoices(new_inv)
        posted_ok = all(m["state"] == "posted" and m["amount_total"] > 0
                        and m["invoice_date_due"] for m in summary)
        return {"_ok": posted_ok, "order": rec["name"], "invoices": summary}

    def _summarize_invoices(self, inv_ids):
        moves = self.c.read("account.move", inv_ids,
                            ["name", "state", "move_type", "amount_untaxed", "amount_tax",
                             "amount_total", "amount_residual", "invoice_date",
                             "invoice_date_due", "invoice_payment_term_id"])
        # per-component tax split: CGST/SGST/IGST appear as separate lines when
        # l10n_in is installed, so the GST breakdown is visible without guessing.
        for m in moves:
            tax_lines = self.c.search_read(
                "account.move.line",
                [("move_id", "=", m["id"]), ("tax_line_id", "!=", False)],
                ["name", "tax_line_id", "balance"])
            m["taxes"] = [{"name": t["name"] or (t["tax_line_id"] and t["tax_line_id"][1]),
                           "amount": abs(t["balance"])} for t in tax_lines]
        return moves

    # --- STOCK / DELIVERY  (FEFO lot selection)  ----------------------------
    # The delivery leg of a sale: reserve stock and validate the outgoing picking.
    # With a lot-tracked product whose category forces the FEFO removal strategy,
    # action_assign reserves the earliest-EXPIRY lot first (First Expiry First Out),
    # exactly what perishable agro-inputs (pesticides, etc.) need. We read the
    # picked lot back so the caller can verify which batch actually shipped.
    FEFO_CATEGORY = "Agro FEFO"

    def _stock_location(self):
        wh = self.c.search_read("stock.warehouse", [], ["lot_stock_id"], limit=1)
        if not wh:
            raise ValueError("No warehouse configured (is the 'stock' module installed?)")
        return wh[0]["lot_stock_id"][0]

    def _ensure_fefo_category(self):
        """Return the id of the FEFO-forced product category, creating it if absent.
        Products filed here inherit removal strategy FEFO (earliest expiry ships first)."""
        cat = self.c.search("product.category", [("name", "=", self.FEFO_CATEGORY)], limit=1)
        if cat:
            return cat[0]
        fefo = self.c.search("product.removal", [("method", "=", "fefo")], limit=1)
        return self.c.create("product.category",
                             {"name": self.FEFO_CATEGORY,
                              "removal_strategy_id": fefo[0] if fefo else False})

    def ensure_fefo_product(self, name, list_price=0.0, use_expiration=True):
        """Ensure a STORABLE, LOT-tracked product in the FEFO-forced category.
        Idempotent (matches by name). Returns product.product id. A product must be
        storable + tracking='lot' for lot/expiry delivery; the category forces FEFO."""
        vals = {"is_storable": True, "tracking": "lot",
                "use_expiration_date": use_expiration,
                "categ_id": self._ensure_fefo_category(), "sale_ok": True}
        found = self.c.search("product.product", [("name", "=", name)], limit=1)
        if found:
            self.c.write("product.product", found, vals)
            return found[0]
        vals.update({"name": name, "type": "consu", "list_price": list_price})
        return self.c.create("product.product", vals)

    @staticmethod
    def _as_datetime(d):
        """Accept 'YYYY-MM-DD' or a full datetime string; return a datetime string."""
        if not d:
            return None
        return d if len(d) > 10 else d + " 00:00:00"

    def add_lot_stock(self, product, lot, qty, expiration_date=None, location=None):
        """Put on-hand stock of a lot-tracked product in via an inventory adjustment,
        creating the lot (with an optional expiry date) if needed. FEFO orders by
        that expiry. Use this to seed/open/receive lot stock so a sale can be
        delivered against it.

        product          : product name (auto-configured for FEFO) or id
        lot              : lot/batch name (str)
        qty              : quantity to set on hand (float)
        expiration_date  : 'YYYY-MM-DD' (or full datetime); optional but drives FEFO
        """
        pid = product if isinstance(product, int) else self.ensure_fefo_product(product)
        loc = location or self._stock_location()
        exp = self._as_datetime(expiration_date)

        lot_ids = self.c.search("stock.lot", [("name", "=", lot), ("product_id", "=", pid)], limit=1)
        if lot_ids:
            if exp:
                self.c.write("stock.lot", lot_ids, {"expiration_date": exp})
            lot_id = lot_ids[0]
        else:
            lvals = {"name": lot, "product_id": pid}
            if exp:
                lvals["expiration_date"] = exp
            lot_id = self.c.create("stock.lot", lvals)

        # Direct quant writes require inventory_mode; action_apply_inventory commits
        # the counted qty as the new on-hand (and returns an empty envelope on success).
        ctx = {"inventory_mode": True}
        q = self.c.search("stock.quant",
                          [("product_id", "=", pid), ("location_id", "=", loc), ("lot_id", "=", lot_id)],
                          limit=1)
        if q:
            self.c.execute_kw("stock.quant", "write", [q, {"inventory_quantity": qty}], {"context": ctx})
        else:
            q = [self.c.execute_kw("stock.quant", "create",
                                   [{"product_id": pid, "location_id": loc,
                                     "lot_id": lot_id, "inventory_quantity": qty}], {"context": ctx})]
        self.c.execute_kw("stock.quant", "action_apply_inventory", [q], {"context": ctx})

        onhand = self.c.search_read("stock.quant", [("id", "in", q)],
                                    ["lot_id", "quantity", "removal_date"])
        return {"_ok": bool(onhand) and onhand[0]["quantity"] == qty,
                "product_id": pid, "lot_id": lot_id, "onhand": onhand}

    def deliver_sale(self, order, auto_confirm=True):
        """Reserve (FEFO) and validate the delivery picking(s) for a sale order, then
        read back which lot(s) actually shipped.

        Confirming a sale of a storable product auto-creates the outgoing picking;
        we confirm first if needed, action_assign (which picks the earliest-expiry
        lot under FEFO), mark the move lines picked, and button_validate with
        skip_backorder so a short-reserve doesn't spawn a backorder wizard.

        Returns per-picking state and the picked lot + its expiry, so the caller can
        verify the correct batch left stock. _ok is True only when every picking
        reaches state 'done'.
        """
        so_id = self._resolve_sale_order(order)
        rec = self.c.read("sale.order", [so_id], ["name", "state", "picking_ids"])[0]

        if rec["state"] in ("draft", "sent"):
            if not auto_confirm:
                return {"_ok": False,
                        "error": f"Order {rec['name']} is '{rec['state']}'; confirm it "
                                 f"first or pass auto_confirm=True."}
            self.c.call("sale.order", "action_confirm", [so_id])
            rec = self.c.read("sale.order", [so_id], ["name", "state", "picking_ids"])[0]

        pickings = rec["picking_ids"]
        if not pickings:
            return {"_ok": False,
                    "error": f"Order {rec['name']} has no delivery picking. Is the product "
                             f"storable (is_storable) and 'stock'/'sale_stock' installed?"}

        results = []
        for pick in pickings:
            prec = self.c.read("stock.picking", [pick], ["state"])[0]
            if prec["state"] not in ("done", "cancel"):
                # reserve; FEFO resolves the lot(s) by earliest removal/expiry date
                self.c.call("stock.picking", "action_assign", [pick])
                mls = self.c.search("stock.move.line", [("picking_id", "=", pick)])
                if mls:
                    self.c.write("stock.move.line", mls, {"picked": True})
                self.c.execute_kw("stock.picking", "button_validate", [[pick]],
                                  {"context": {"skip_backorder": True, "skip_sms": True}})
            results.append(self._summarize_picking(pick))

        return {"_ok": all(r["state"] == "done" for r in results),
                "order": rec["name"], "pickings": results}

    def _summarize_picking(self, pick):
        prec = self.c.read("stock.picking", [pick], ["name", "state", "scheduled_date"])[0]
        lines = self.c.search_read("stock.move.line", [("picking_id", "=", pick)],
                                   ["product_id", "lot_id", "quantity", "picked"])
        # pull expiry from the lots (avoids assuming move-line has the field)
        lot_ids = [l["lot_id"][0] for l in lines if l["lot_id"]]
        exp = {}
        if lot_ids:
            for lt in self.c.read("stock.lot", lot_ids, ["expiration_date", "removal_date"]):
                exp[lt["id"]] = lt
        prec["moves"] = [{"product": l["product_id"] and l["product_id"][1],
                          "lot": l["lot_id"] and l["lot_id"][1],
                          "qty": l["quantity"], "picked": l["picked"],
                          "expiration_date": (exp.get(l["lot_id"][0], {}).get("expiration_date")
                                              if l["lot_id"] else None)}
                         for l in lines]
        return prec

    # --- PRODUCT MASTER  (HSN + GST split + lot/expiry + UoM/pack) -----------
    # Create a product.template the way an agro-distributor needs it: an HSN/SAC
    # code, a GST rate that resolves to the correct tax objects (intra-state ->
    # CGST+SGST group, inter-state -> IGST), optional lot tracking with expiry
    # (auto-filed in the FEFO category so deliveries ship earliest-expiry first),
    # a base UoM, and optional pack/case UoMs. Only intent goes in; Odoo computes
    # the tax_string and we read the record back to verify what actually landed.
    def _has_field(self, model, field):
        try:
            return field in self.c.execute_kw(model, "fields_get", [[field]],
                                              {"attributes": ["type"]})
        except Exception:  # noqa: BLE001
            return False

    def _resolve_uom(self, uom):
        """Resolve a uom.uom by id or name (exact, then contains). Returns id or None."""
        if isinstance(uom, int):
            return uom
        found = self.c.search("uom.uom", [("name", "=", uom)], limit=1)
        if not found:
            found = self.c.search("uom.uom", [("name", "ilike", uom)], limit=1)
        return found[0] if found else None

    def _tax_defaults(self):
        """Borrow the REQUIRED tax fields (company_id, country_id, tax_group_id)
        from an existing tax so new GST taxes are valid AND selectable on this
        company (the taxes_id domain filters by the company's fiscal country)."""
        t = self.c.search_read("account.tax", [("type_tax_use", "in", ["sale", "purchase"])],
                               ["company_id", "country_id", "tax_group_id"], limit=1)
        if not t:
            raise ValueError("No existing account.tax to borrow required fields from.")
        d = t[0]
        return {"company_id": d["company_id"][0], "country_id": d["country_id"][0],
                "tax_group_id": d["tax_group_id"][0]}

    def ensure_gst_tax(self, rate, type_tax_use="sale", interstate=False):
        """Resolve a GST tax of `rate`% and return its id.

        Prefers the OFFICIAL India localization tax (l10n_in chart), which the
        chart ships INACTIVE:
          - intra-state (default): '{rate}% GST S|P' (group -> CGST + SGST),
          - inter-state:           '{rate}% IGST S|P' (single IGST).
        We activate the resolved tax (and its group children) so it becomes
        selectable. Falls back to hand-building a CGST/SGST group (or IGST) tax
        when the India chart isn't present (e.g. a generic_coa company)."""
        suffix = "S" if type_tax_use == "sale" else "P"
        r = f"{rate:g}"
        official = f"{r}% {'IGST' if interstate else 'GST'} {suffix}"
        found = self.c.search("account.tax",
                              [("name", "=", official), ("type_tax_use", "=", type_tax_use)],
                              context={"active_test": False}, limit=1)
        if found:
            tax = self.c.read("account.tax", found, ["active", "children_tax_ids"])[0]
            to_activate = ([] if tax.get("active") else [found[0]])
            kids = tax.get("children_tax_ids") or []
            if kids:
                to_activate += [k["id"] for k in self.c.read("account.tax", kids, ["active"])
                                if not k.get("active")]
            if to_activate:
                self.c.write("account.tax", to_activate, {"active": True})
            return found[0]

        # ---- fallback: hand-build (company without the India GST chart) ----
        base = self._tax_defaults()
        side = "Sales" if type_tax_use == "sale" else "Purchase"
        # format numbers cleanly so 18 and 18.0 map to the same canonical tax name
        r, h = f"{rate:g}", f"{rate / 2.0:g}"

        def _get_or_create(name, vals, ttu):
            found = self.c.search("account.tax",
                                  [("name", "=", name), ("type_tax_use", "=", ttu)], limit=1)
            if found:
                return found[0]
            return self.c.create("account.tax", {**base, "name": name,
                                                 "type_tax_use": ttu, **vals})

        if interstate:
            return _get_or_create(f"IGST {r}% {side}",
                                  {"amount": rate, "amount_type": "percent"}, type_tax_use)

        half = rate / 2.0
        cgst = _get_or_create(f"CGST {h}%", {"amount": half, "amount_type": "percent"}, "none")
        sgst = _get_or_create(f"SGST {h}%", {"amount": half, "amount_type": "percent"}, "none")
        return _get_or_create(f"GST {r}% (CGST+SGST) {side}",
                              {"amount_type": "group",
                               "children_tax_ids": [(6, 0, [cgst, sgst])]}, type_tax_use)

    def ensure_pack_uom(self, name, contains, base_uom_id):
        """Ensure a pack/case UoM that 'contains' N base units. Idempotent by name.
        Odoo 19 uses relative units: relative_uom_id + relative_factor."""
        found = self.c.search("uom.uom", [("name", "=", name)], limit=1)
        if found:
            return found[0]
        return self.c.create("uom.uom", {"name": name,
                                         "relative_uom_id": base_uom_id,
                                         "relative_factor": contains})

    def create_product(self, name, hsn=None, gst_rate=None, uom="Units",
                       tracking=None, use_expiration=False, list_price=None,
                       standard_price=None, category=None, pack=None,
                       interstate_gst=False, sale_ok=True, purchase_ok=True,
                       internal_reference=None, storable=False):
        """Create (or update, idempotent by name) a product.template.

        name           : product name.
        internal_reference : product code (default_code), e.g. 'AANDHI-1L'.
        storable       : track quantity on hand (is_storable) WITHOUT lot/serial.
                         Ignored when tracking='lot'/'serial' (those force storable).
        hsn            : HSN/SAC code (stored in l10n_in_hsn_code; needs l10n_in).
        gst_rate       : GST % -> resolves sale & purchase taxes (CGST/SGST split,
                         or IGST if interstate_gst=True). Omit to leave taxes alone.
        uom            : base unit of measure name/id (default 'Units').
        tracking       : 'lot' | 'serial' | 'none'. 'lot'/'serial' force storable.
        use_expiration : enable expiry dates (perishable agro-inputs).
        list_price / standard_price : sales price / cost. None = leave default (0).
        category       : product.category name; defaults to the FEFO category when
                         lot-tracked + expiry (so deliver_sale ships earliest expiry).
        pack           : optional {'name': 'Case of 10', 'contains': 10} pack UoM.
        Reads the record back and verifies name/uom/hsn/tax/tracking landed."""
        vals = {"name": name, "sale_ok": sale_ok, "purchase_ok": purchase_ok}

        uom_id = self._resolve_uom(uom) if uom else None
        if uom_id:
            vals["uom_id"] = uom_id

        if internal_reference is not None:
            vals["default_code"] = str(internal_reference)

        if tracking in ("lot", "serial"):
            vals.update({"type": "consu", "is_storable": True, "tracking": tracking})
        elif tracking == "none":
            vals["tracking"] = "none"
        if storable and tracking not in ("lot", "serial"):
            vals.update({"type": "consu", "is_storable": True})
        if use_expiration:
            vals["use_expiration_date"] = True

        if category:
            cat = self.c.search("product.category", [("name", "=", category)], limit=1)
            if cat:
                vals["categ_id"] = cat[0]
        elif tracking == "lot" and use_expiration:
            vals["categ_id"] = self._ensure_fefo_category()

        if hsn is not None and self._has_field("product.template", "l10n_in_hsn_code"):
            vals["l10n_in_hsn_code"] = str(hsn)

        if gst_rate:
            vals["taxes_id"] = [(6, 0, [self.ensure_gst_tax(gst_rate, "sale", interstate_gst)])]
            vals["supplier_taxes_id"] = [(6, 0, [self.ensure_gst_tax(gst_rate, "purchase", interstate_gst)])]

        if list_price is not None:
            vals["list_price"] = list_price
        if standard_price is not None:
            vals["standard_price"] = standard_price

        found = self.c.search("product.template", [("name", "=", name)], limit=1)
        if found:
            self.c.write("product.template", found, vals)
            tid = found[0]
        else:
            tid = self.c.create("product.template", vals)

        if pack:
            puom = self.ensure_pack_uom(pack["name"], pack["contains"],
                                        uom_id or self._resolve_uom("Units"))
            cur = self.c.read("product.template", [tid], ["uom_ids"])[0]["uom_ids"]
            self.c.write("product.template", [tid],
                         {"uom_ids": [(6, 0, list({*cur, puom}))]})

        summary = self._summarize_product(tid)
        # verification: intent actually landed
        summary["_ok"] = bool(
            summary["name"] and summary.get("uom_id")
            and (not gst_rate or summary["sales_taxes"])
            and (hsn is None or not self._has_field("product.template", "l10n_in_hsn_code")
                 or summary.get("l10n_in_hsn_code") == str(hsn))
            and (tracking not in ("lot", "serial") or summary.get("tracking") == tracking)
            and (internal_reference is None or summary.get("default_code") == str(internal_reference))
            and (not storable or summary.get("is_storable")))
        return summary

    def _summarize_product(self, tid):
        fields = ["name", "default_code", "type", "is_storable", "tracking",
                  "use_expiration_date", "uom_id", "uom_ids", "list_price",
                  "standard_price", "categ_id", "taxes_id", "supplier_taxes_id", "tax_string"]
        if self._has_field("product.template", "l10n_in_hsn_code"):
            fields.append("l10n_in_hsn_code")
        rec = self.c.read("product.template", [tid], fields)[0]
        rec["id"] = tid

        def _tax_detail(ids):
            out = []
            for t in self.c.read("account.tax", ids,
                                 ["name", "amount", "amount_type", "children_tax_ids"]):
                kids = (self.c.read("account.tax", t["children_tax_ids"], ["name", "amount"])
                        if t["children_tax_ids"] else [])
                out.append({"name": t["name"], "amount": t["amount"], "type": t["amount_type"],
                            "components": [{"name": k["name"], "amount": k["amount"]} for k in kids]})
            return out

        rec["sales_taxes"] = _tax_detail(rec.get("taxes_id", []))
        rec["purchase_taxes"] = _tax_detail(rec.get("supplier_taxes_id", []))
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
