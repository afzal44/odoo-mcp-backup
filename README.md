# odoo-mcp-backup

Dedicated, sanitized backup of the **Odoo Functional MCP** module source
(`/root/.openclaw/workspace-odoo-ops/odoo_agent`) — the JSON-RPC semantic layer
+ MCP server that lets an AI agent record sales/purchases (and, over time, more)
against Odoo 19.

## What's backed up (`snapshot/`)
Text/code only, via an extension allowlist: `*.py *.md *.json *.txt *.toml
*.cfg *.ini *.example`, plus a generated `MANIFEST.txt`.

## What's deliberately NOT backed up
- `.venv/` — regenerable; see restore below.
- `.mlrd_agent_key` — **live Odoo API credential**, force-excluded and blocked by
  a hard secret gate. Never commit it here.
- caches / binaries.

## Safety
`backup.sh` hard-aborts (commits nothing) if the snapshot contains token-shaped
material, any `apikey=/password=`-style assignment, the **actual live API key
value**, or any binary. Same discipline as the other two box backups.

## Usage
```bash
./backup.sh            # stage + secret-scan + local commit
./backup.sh --push     # + push to origin (cron)
./backup.sh --no-push  # dry run, no commit
```

## Restore
```bash
# 1. copy source back
cp -a snapshot/. /root/.openclaw/workspace-odoo-ops/odoo_agent/
# 2. rebuild the venv
cd /root/.openclaw/workspace-odoo-ops/odoo_agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 3. recreate .mlrd_agent_key (API key for the mlrd 'ai_agent' user) — NOT in this repo
```

Auth: local `credential.helper=store` → `/root/.git-credentials` (same token as
the box's other backups). Cron suggestion: `30 3 * * *` (after the Odoo backup).
