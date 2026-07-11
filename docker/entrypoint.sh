#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# Synapse container entrypoint — internal-token bootstrap
# ─────────────────────────────────────────────────────────────────────────────
# Ensures SYNAPSE_INTERNAL_TOKEN is always set in Docker deployments so the
# backend's InternalTokenMiddleware enforces (never silently permissive).
#
# Behaviour (only when SYNAPSE_AUTOGEN_TOKEN=1 and no token is already set):
#   - SYNAPSE_TOKEN_MODE=generate (backend / single image): generate a random
#     token once and persist it to $SYNAPSE_SECRETS_DIR/internal_token, then
#     export it. Requires `python` (present in the backend images).
#   - SYNAPSE_TOKEN_MODE=read (frontend image, node:alpine, no python): wait
#     briefly for the backend-written token file on the shared volume, then
#     export the same value so proxy.ts can inject it. Never generates.
#
# Persisting to a volume means the token survives restarts and is shared between
# the frontend and backend containers (compose) or between the two supervisord
# programs (single image). An operator can still pin a token by setting
# SYNAPSE_INTERNAL_TOKEN explicitly — in that case this script is a no-op.
# ─────────────────────────────────────────────────────────────────────────────
set -e

: "${SYNAPSE_SECRETS_DIR:=/data}"
: "${SYNAPSE_TOKEN_MODE:=generate}"   # generate | read
TOKEN_FILE="$SYNAPSE_SECRETS_DIR/internal_token"

# Emit 64 hex chars using whatever is available in the image (Debian slim has
# python/openssl; alpine has busybox od). Kept dependency-free on purpose.
_gen_token() {
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import secrets; print(secrets.token_hex(32))'
    elif command -v python >/dev/null 2>&1; then
        python -c 'import secrets; print(secrets.token_hex(32))'
    elif command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
    fi
}

if [ "$SYNAPSE_AUTOGEN_TOKEN" = "1" ] && [ -z "$SYNAPSE_INTERNAL_TOKEN" ]; then
    if [ "$SYNAPSE_TOKEN_MODE" = "generate" ] && [ ! -s "$TOKEN_FILE" ]; then
        # Only the generator role mints the token. The frontend (read mode) just
        # waits for the file below. Write via a temp file + atomic mv so a failed
        # generation never leaves a truncated/empty token behind.
        mkdir -p "$SYNAPSE_SECRETS_DIR"
        _tmp="$TOKEN_FILE.tmp.$$"
        ( umask 077; _gen_token > "$_tmp" )
        if [ -s "$_tmp" ]; then
            mv "$_tmp" "$TOKEN_FILE"
        else
            rm -f "$_tmp"
        fi
    fi

    # Wait up to ~10s for the token file to appear (covers the frontend racing
    # ahead of the backend on first boot).
    i=0
    while [ ! -s "$TOKEN_FILE" ] && [ "$i" -lt 50 ]; do
        sleep 0.2
        i=$((i + 1))
    done

    SYNAPSE_INTERNAL_TOKEN="$(cat "$TOKEN_FILE" 2>/dev/null || true)"
    export SYNAPSE_INTERNAL_TOKEN

    if [ -z "$SYNAPSE_INTERNAL_TOKEN" ]; then
        echo "[entrypoint] WARNING: could not establish an internal token; backend will run in loopback-only mode." >&2
    else
        echo "[entrypoint] Internal token ready (source: $TOKEN_FILE)."
    fi
fi

exec "$@"
