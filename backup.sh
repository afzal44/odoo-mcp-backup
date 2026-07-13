#!/usr/bin/env bash
# =============================================================================
# Odoo Functional MCP — DEDICATED source backup (sanitized, text-only, restorable)
# -----------------------------------------------------------------------------
# Backs up ONLY the MCP module's source from:
#   /root/.openclaw/workspace-odoo-ops/odoo_agent
# Text/code allowlist only. Regenerates snapshot/, HARD-ABORTS if any secret
# (token shapes OR the live Odoo API key value) survives, then commits.
# Push only when called with --push.
#
# NEVER included: .venv/ (rebuild from requirements.txt), .mlrd_agent_key
# (live credential — lives one dir up and is force-excluded + gate-checked),
# __pycache__, *.pyc, any binary.
#
#   ./backup.sh            # stage + secret-scan + local commit
#   ./backup.sh --push     # same, then push to origin (used by cron)
#   ./backup.sh --no-push  # dry run (stage + scan, no commit)
# =============================================================================
set -euo pipefail

SRC="/root/.openclaw/workspace-odoo-ops/odoo_agent"
KEYFILE="/root/.openclaw/workspace-odoo-ops/.mlrd_agent_key"
REPO_DIR="/root/odoo-mcp-backup"
SNAP="$REPO_DIR/snapshot"
MODE="${1:-}"

# Unambiguous secret/key material scanned across the whole snapshot.
TOKEN_RE='ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{18,}\.'
# Odoo-shaped credential lines (the general gate misses these).
CRED_RE='(apikey|api_key|admin_passwd|db_password|password|passwd|secret|token)[[:space:]]*=[[:space:]]*\S{12,}'

echo "==> [1/4] Reset snapshot"
[ -d "$SRC" ] || { echo "!! source missing: $SRC" >&2; exit 1; }
rm -rf "$SNAP"
mkdir -p "$SNAP"

echo "==> [2/4] Staging MCP source (text/code allowlist)"
# Allowlist copy: only source/text. .venv, caches, key file, binaries excluded.
rsync -rL --prune-empty-dirs \
  --exclude='.venv/' --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='.mlrd_agent_key' \
  --include='*/' \
  --include='*.py' --include='*.md' --include='*.json' --include='*.txt' \
  --include='*.toml' --include='*.cfg' --include='*.ini' --include='*.example' \
  --exclude='*' \
  "$SRC"/ "$SNAP"/

echo "==> [3/4] HARD GATE: scanning snapshot for secrets"
fail=0
tok=$(grep -rIlP "$TOKEN_RE" "$SNAP" 2>/dev/null || true)
[ -n "$tok" ] && { echo "!! ABORT: token/key material:" >&2; echo "$tok" >&2; fail=1; }
cred=$(grep -rIlP "$CRED_RE" "$SNAP" 2>/dev/null || true)
[ -n "$cred" ] && { echo "!! ABORT: credential-shaped assignment:" >&2; echo "$cred" >&2; fail=1; }
# Belt-and-braces: never let the ACTUAL live key value slip in.
if [ -f "$KEYFILE" ]; then
  live=$(grep -E '^apikey=' "$KEYFILE" | cut -d= -f2- || true)
  if [ -n "$live" ] && grep -rIlF "$live" "$SNAP" >/dev/null 2>&1; then
    echo "!! ABORT: live API key value present in snapshot" >&2; fail=1
  fi
fi
# Binary guard (NUL byte).
bin=$(LC_ALL=C grep -rlP '\x00' "$SNAP" 2>/dev/null || true)
[ -n "$bin" ] && { echo "!! ABORT: binary file(s):" >&2; echo "$bin" >&2; fail=1; }
if [ "$fail" != "0" ]; then echo "!! Nothing committed/pushed." >&2; exit 1; fi
echo "    clean (no secrets, no binaries)."

# manifest
{
  echo "# Odoo Functional MCP backup snapshot"
  echo "# generated $(date -u '+%F %H:%M UTC')"
  echo "# size: $(du -sh "$SNAP" | cut -f1)"
  echo
  ( cd "$SNAP" && find . -type f | sort )
} > "$SNAP/MANIFEST.txt"

echo "==> [4/4] Commit"
cd "$REPO_DIR"
if [ "$MODE" = "--no-push" ]; then
  echo "    dry run: staged, gate clean, size $(du -sh "$SNAP" | cut -f1). No commit."
  exit 0
fi
git add -A
if git diff --cached --quiet; then echo "    No changes since last backup."; exit 0; fi
git commit -q -m "MCP source backup - $(date -u '+%F %H:%M UTC')"
echo "    committed."
if [ "$MODE" = "--push" ]; then
  if git remote get-url origin >/dev/null 2>&1; then
    git push -q origin HEAD && echo "    pushed."
  else
    echo "    (no 'origin' remote yet — add it, then re-run with --push)"
  fi
else
  echo "    (local commit only; run with --push to publish)"
fi
