"""One-shot importer: WCPS product sheet -> mlrd (dev) via the functional layer.

Reads sheet 'Products_Import', maps columns to create_product, creates/updates
each product.template (idempotent by name), reads back and reports _ok.

Usage:  python3 import_wcps.py <xlsx> [--limit N] [--dry]
"""
import os
import re
import sys

import openpyxl
from odoo_client import from_keyfile
from functional_layer import FunctionalAgent

KEYFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".mlrd_agent_key")


def parse_gst(text):
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    return float(m.group(1)) if m else None


def first_hsn(text):
    if not text:
        return None
    # "2810 / 3105" -> "2810"
    return str(text).split("/")[0].strip() or None


def rows_from(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Products_Import"]
    it = ws.iter_rows(values_only=True)
    header = [h.strip() if isinstance(h, str) else h for h in next(it)]
    idx = {h: i for i, h in enumerate(header)}
    for r in it:
        if not r or not r[idx["Name"]]:
            continue
        yield {
            "name": str(r[idx["Name"]]).strip(),
            "ref": (str(r[idx["Internal Reference"]]).strip()
                    if r[idx["Internal Reference"]] else None),
            "hsn": first_hsn(r[idx["HSN Code"]]),
            "gst": parse_gst(r[idx["Customer Taxes"]]),
            "storable": bool(r[idx["Track Inventory"]]),
        }


def main():
    args = sys.argv[1:]
    path = args[0]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    dry = "--dry" in args

    recs = list(rows_from(path))
    if limit:
        recs = recs[:limit]

    if dry:
        for r in recs:
            print(r)
        print(f"\n{len(recs)} rows parsed (dry run, nothing sent).")
        return

    client = from_keyfile(KEYFILE)
    client.login()
    agent = FunctionalAgent(client)
    print(f"Connected to {client.db}. Importing {len(recs)} products...\n")

    ok = fail = 0
    for i, r in enumerate(recs, 1):
        res = agent.create_product(
            name=r["name"],
            internal_reference=r["ref"],
            hsn=r["hsn"],
            gst_rate=r["gst"],
            storable=r["storable"],
            interstate_gst=False,
        )
        good = res.get("_ok")
        ok += bool(good)
        fail += (not good)
        tax = (res.get("sales_taxes") or [{}])[0].get("name", "-")
        print(f"[{i:2}/{len(recs)}] {'OK ' if good else 'ERR'} id={res.get('id')} "
              f"{r['name']!r} ref={res.get('default_code')} hsn={res.get('l10n_in_hsn_code')} "
              f"storable={res.get('is_storable')} tax={tax}")
        if not good:
            print(f"       -> summary: name={res.get('name')} err={res.get('error')}")

    print(f"\nDone. {ok} ok, {fail} not-ok, {len(recs)} total.")


if __name__ == "__main__":
    main()
