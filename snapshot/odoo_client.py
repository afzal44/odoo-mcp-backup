"""
Thin JSON-RPC client for Odoo's external API.

This is the *transport* layer only. It knows nothing about sales or purchases —
it just authenticates and relays execute_kw calls over /jsonrpc. The business
correctness lives one layer up, in functional_layer.py.

Same endpoint an MCP server would use under the hood. Stdlib only (urllib) so it
runs anywhere with zero pip installs.
"""
import json
import urllib.request


class OdooError(RuntimeError):
    """Raised when Odoo returns a JSON-RPC error payload."""


class OdooClient:
    def __init__(self, url, db, login, password):
        self.url = url.rstrip("/")
        self.db = db
        self.username = login
        self.password = password  # an API key works in place of the password
        self.uid = None

    # --- low level -----------------------------------------------------------
    def _call(self, service, method, args):
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }
        req = urllib.request.Request(
            self.url + "/jsonrpc",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
        if body.get("error"):
            err = body["error"]
            data = err.get("data", {})
            raise OdooError(f"{data.get('name', err.get('message'))}: {data.get('message', '')}".strip())
        # Some Odoo action methods (e.g. stock.quant.action_apply_inventory) return
        # an empty JSON-RPC envelope with no "result" key on success. Treat a missing
        # result as None rather than raising KeyError.
        return body.get("result")

    def login(self):
        self.uid = self._call("common", "authenticate", [self.db, self.username, self.password, {}])
        if not self.uid:
            raise OdooError("Authentication failed - check login / API key / dbfilter")
        return self.uid

    def execute_kw(self, model, method, args, kwargs=None):
        if self.uid is None:
            self.login()
        return self._call(
            "object", "execute_kw",
            [self.db, self.uid, self.password, model, method, args, kwargs or {}],
        )

    # --- convenience wrappers ------------------------------------------------
    def search(self, model, domain, **kw):
        return self.execute_kw(model, "search", [domain], kw)

    def search_read(self, model, domain, fields=None, **kw):
        if fields is not None:
            kw["fields"] = fields
        return self.execute_kw(model, "search_read", [domain], kw)

    def read(self, model, ids, fields=None):
        return self.execute_kw(model, "read", [ids], {"fields": fields} if fields else {})

    def create(self, model, vals):
        return self.execute_kw(model, "create", [vals])

    def write(self, model, ids, vals):
        return self.execute_kw(model, "write", [ids, vals])

    def call(self, model, method, ids, *args):
        """Call an arbitrary model method on a recordset (e.g. action_confirm)."""
        return self.execute_kw(model, method, [ids, *args])


def from_keyfile(path):
    """Load connection settings from the root-only .mlrd_agent_key file."""
    cfg = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return OdooClient(cfg["url"], cfg["db"], cfg["login"], cfg["apikey"])
