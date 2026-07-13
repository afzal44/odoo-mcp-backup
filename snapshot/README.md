# odoo_agent — functional-layer scaffold (Odoo 19)

A minimal, runnable example of the **right way** to let an AI agent record
business transactions (sales, purchases) in Odoo: a thin transport client + a
**semantic functional layer**, instead of exposing raw CRUD to an LLM.

Proven end-to-end against **mlrd (DEV)** on 2026-07-13.

## Files
| File | Role |
|---|---|
| `odoo_client.py` | Transport only — JSON-RPC over `/jsonrpc`, auth, `execute_kw`. Stdlib. |
| `functional_layer.py` | Domain logic — `record_sale` / `record_purchase` that encapsulate the correct Odoo flow. **Grows over time.** |
| `mcp_server.py` | The MCP server — wraps the functional layer as safe, named tools. **Grows over time.** |
| `test_mcp.py` | Real MCP-protocol test (spawns server over stdio, calls tools). |
| `demo.py` | Direct (non-MCP) run of the functional layer. |
| `.venv/` | Isolated venv with the `mcp` SDK (system Python untouched). |
| `mcp_client_config.example.json` | Drop-in config to connect Claude Desktop / Claude Code. |
| `../.mlrd_agent_key` | Connection + API key. **root-only (0600), never commit, never print.** |

## Run
```bash
cd odoo_agent
python3 demo.py                       # direct functional layer (no MCP)
.venv/bin/python mcp_server.py        # start the MCP server (stdio)
.venv/bin/python test_mcp.py          # prove it over the real MCP protocol
```

## How we grow this — the repeatable process
Every new capability follows the same 5 steps (agreed 2026-07-13):

1. **CHECK**  — Boss names a need ("upload documents"). Ops confirms the Odoo
   models/flow on **dev (mlrd) first** — which model, required fields, workflow.
2. **BUILD** — add a method to `functional_layer.py` doing the *correct* flow
   (compute-on-create / real workflow methods / read-back verify).
3. **PLAN**  — decide the MCP tool's name, inputs, and what it verifies.
4. **TEST**  — add a case to `test_mcp.py`; prove it over real MCP on dev.
5. **WRAP**  — add one `@mcp.tool()` block to `mcp_server.py` under a new
   `# CAPABILITY:` section.

Tools shipped so far: `record_sale`, `record_purchase`, `find_partner`,
`list_products`. Next up (Boss's example): document upload.

## Connect an MCP client
See `mcp_client_config.example.json`. It points the client at the venv Python +
`mcp_server.py` over stdio.

## The core idea (why this beats generic MCP CRUD)
An LLM given raw `create`/`write` will hand-set prices, miss taxes, and poke
state fields. This layer instead:

1. **Supplies only trigger fields** — `partner_id`, `product_id`, `qty`. In
   Odoo 17+/19 the price/tax/description fields on order lines are *computed,
   stored* fields, so `create()` makes the server compute prices & taxes.
2. **Runs the real workflow** — `action_confirm` / `button_confirm`, not a
   manual `state` write (which would skip stock/accounting side effects).
3. **Reads back and verifies** — returns `amount_total` etc. with an `_ok`
   guard (`total > 0`) so the agent can detect a mispriced/broken order.

The agent supplies *intent*; Odoo supplies *correctness*.

## The MCP server (built & tested)
`mcp_server.py` wraps the functional layer as MCP tools — the agent sees
**safe, named, semantic tools**, not raw CRUD. Same engine (JSON-RPC), better
guardrails. Verified over the real MCP protocol via `test_mcp.py` on 2026-07-13
(record_sale → S00003 / 5750, record_purchase → P00002 / 5520). To add a tool,
follow the 5-step process above.

## Safety notes / decisions baked in
- Runs as a **dedicated scoped user** `ai_agent` (uid 33 on mlrd), NOT admin.
  Groups: internal user + Sales Manager + Purchase Manager + Product Manager +
  Invoicing. Scope the groups down further for prod.
- Auth via **API key** (works in place of password over JSON-RPC), stored 0600.
- This Odoo 19 build enforces an **API-key max lifespan of 90 days** for
  non-`group_system` users — the scaffold key expires **2026-10-10**; regenerate
  before then (Settings → Users → ai_agent → API Keys, or re-run the generator).
- **POS is deliberately NOT here.** `pos.order` must go through the POS session
  sync flow (open session, payments, settlement); record back-office sales as
  `sale.order` instead.
- Field renames in Odoo 19 that bit us: `res.users.groups_id`→`group_ids`,
  `sale.order.line.tax_id`→`tax_ids`.

## Prod checklist before reusing on wcits/ecom
- Dedicated user with **least-privilege** groups (read-only where possible).
- Consider **read-only first**; enable write per-model only when proven.
- Log every agent write (Odoo audit / a wrapper log).
- Never point a generic full-CRUD MCP at prod — only this semantic layer.
