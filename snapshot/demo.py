"""
End-to-end demo of the functional layer against mlrd (DEV).

Run:  python3 demo.py
It authenticates with the root-only API key, records a sale + a purchase using
only business intent, and prints the server-computed totals so you can see that
prices & taxes were filled in correctly without any hand-pricing.
"""
import json
import os

from odoo_client import from_keyfile
from functional_layer import FunctionalAgent

KEYFILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".mlrd_agent_key")


def main():
    client = from_keyfile(KEYFILE)
    uid = client.login()
    print(f"Authenticated to {client.db} as uid={uid}\n")

    agent = FunctionalAgent(client)

    # seed a demo product with a sales price and a cost
    agent.find_or_create_product("Scaffold Widget", list_price=1000.0, standard_price=600.0)

    print("== record_sale ==")
    so = agent.record_sale(
        customer="Scaffold Demo Customer",
        lines=[("Scaffold Widget", 3)],
        confirm=True,
    )
    print(json.dumps(so, indent=2, default=str))
    print(f"-> {so['name']} state={so['state']} total={so['amount_total']} ok={so['_ok']}\n")

    print("== record_purchase ==")
    po = agent.record_purchase(
        vendor="Scaffold Demo Vendor",
        lines=[("Scaffold Widget", 10)],  # price omitted -> computed from cost
        confirm=True,
    )
    print(json.dumps(po, indent=2, default=str))
    print(f"-> {po['name']} state={po['state']} total={po['amount_total']} ok={po['_ok']}")


if __name__ == "__main__":
    main()
