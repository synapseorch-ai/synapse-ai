#!/bin/bash
# A fresh install, the way a user runs it, inside a clean container.
#
#   docker run --rm -i -v "$DIST:/w:ro" ubuntu:24.04 bash /w/fresh-install.sh
#
# $DIST holds setup.sh, synapse-install.pyz and the wheel (a stand-in for the
# release). The machine has no Python and no Node: the installer brings both.
# Exits non-zero unless Synapse ends up running and its link answers.
set -u
apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates >/dev/null 2>&1
useradd -m me
mkdir -p /cache && chown me /cache
cp /w/setup.sh /home/me/setup.sh && chown me /home/me/setup.sh
WHEEL=$(ls /w/*.whl | head -1)

su me -c "cd ~ && UV_CACHE_DIR=/cache SYNAPSE_INSTALLER=/w/synapse-install.pyz \
  SYNAPSE_PACKAGE=$WHEEL bash setup.sh --yes --no-browser" || { echo "INSTALL FAILED"; exit 1; }

su me <<'AS_ME'
set -u
fail() { echo "FAIL: $*"; tail -n 40 ~/.synapse/logs/*.log; exit 1; }
test -f ~/.synapse/install.json || fail "no install.json"
PY=$(sed -n 's/.*"tool_python": "\([^"]*\)".*/\1/p' ~/.synapse/install.json)
STATUS=$("$PY" -m synapse status --json | tail -1)
echo "$STATUS"
LINK=$("$PY" -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["ready"], s; print(s["link"])' "$STATUS") \
  || fail "not ready"
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$LINK")
[ "$CODE" = "200" ] || [ "$CODE" = "307" ] || [ "$CODE" = "302" ] || fail "link answered $CODE"
echo "OK: $LINK answered $CODE"
AS_ME
