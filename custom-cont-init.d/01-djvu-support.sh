#!/usr/bin/with-contenv bash
# DJVU support for CWA ingest:
#  1) install djvulibre (ddjvu) if missing
#  2) add 'djvu' to the ingest service's SUPPORTED_EXT_REGEX
set -e

# 1. Install djvulibre-bin so the ingest_processor can convert DJVU -> PDF
if ! command -v ddjvu >/dev/null 2>&1; then
    echo "[djvu-support] Installing djvulibre-bin..."
    apt-get update >/dev/null 2>&1 || true
    apt-get install -y --no-install-recommends djvulibre-bin >/dev/null 2>&1 \
        || echo "[djvu-support] WARN: apt-get install failed (offline?), DJVU conversion will not work"
fi

# 2. Add djvu to SUPPORTED_EXT_REGEX in the cwa-ingest-service run script
RUN_SCRIPT="/etc/s6-overlay/s6-rc.d/cwa-ingest-service/run"
if [ -f "$RUN_SCRIPT" ] && ! grep -q '|djvu)' "$RUN_SCRIPT"; then
    sed -i "s/|kfx-zip)/|kfx-zip|djvu)/" "$RUN_SCRIPT"
    echo "[djvu-support] Added djvu to SUPPORTED_EXT_REGEX"
else
    echo "[djvu-support] djvu already present in SUPPORTED_EXT_REGEX (or run script missing)"
fi
