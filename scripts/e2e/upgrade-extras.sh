#!/bin/bash
# An install with Knowledge keeps it through an upgrade, with PyTorch's CPU build
# both times — inside a clean container.
#
#   docker run --rm -i -v "$DIST_A:/a:ro" -v "$DIST_B:/b:ro" ubuntu:24.04 bash /b/upgrade-extras.sh
#
# $DIST_A and $DIST_B each hold setup.sh, synapse-install.pyz and a wheel: the
# release installed first and the one upgraded to (stamped two versions apart).
# The upgrade runs B's installer the way `synapse upgrade` hands over to it.
# Before IU1 the upgrade reinstalled without [knowledge]: torch and docling gone.
set -u
apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates >/dev/null 2>&1
useradd -m me
mkdir -p /cache && chown me /cache
export A_WHEEL=$(ls /a/*.whl | head -1) B_WHEEL=$(ls /b/*.whl | head -1)
export B_VERSION=$(basename "$B_WHEEL" | cut -d- -f2)

su me --preserve-environment -c 'bash -s' <<'AS_ME'
set -u
HOME=/home/me
cd ~
export UV_CACHE_DIR=/cache
# The browser step is not under test; an unreachable download host skips it at once.
export PLAYWRIGHT_DOWNLOAD_HOST=http://127.0.0.1:9
fail() { echo "FAIL: $*"; tail -n 40 ~/.synapse/logs/install.log; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"; echo "ok  $1"; }

cp /a/setup.sh ~/setup.sh
SYNAPSE_INSTALLER=/a/synapse-install.pyz SYNAPSE_PACKAGE=$A_WHEEL \
  bash setup.sh --yes --no-start --no-browser --with knowledge >/dev/null 2>&1 || fail "the install failed"

UV=$(sed -n 's/.*"uv": "\([^"]*\)".*/\1/p' ~/.synapse/install.json)
PY=$(sed -n 's/.*"tool_python": "\([^"]*\)".*/\1/p' ~/.synapse/install.json)
TOOL=$(dirname "$(dirname "$PY")")
parts() {
  "$PY" - <<'PYCODE'
import importlib.metadata as m, pathlib, sys
def v(d):
    try: return m.version(d)
    except m.PackageNotFoundError: return "missing"
nvidia = sum(1 for d in m.distributions() if (d.metadata["Name"] or "").lower().startswith("nvidia"))
print(v("synapse-orch-ai"), v("torch"), v("docling-slim"), nvidia)
PYCODE
}

read -r version torch docling nvidia <<<"$(parts)"
check "installed with knowledge: torch is the CPU build" "${torch##*+}" cpu
check "installed with knowledge: no CUDA libraries" "$nvidia" 0
[ "$docling" != missing ] || fail "docling-slim is missing after --with knowledge"
grep -q 'extras = \["knowledge"\]' "$TOOL/uv-receipt.toml" || fail "the receipt does not name the extra"
# Docling's models, fetched by the install (Knowledge never downloads at run time).
docling_on() {
  SYNAPSE_HOME=~/.synapse "$PY" -P -c 'from synapse import _store; _store.prepare()
from core.knowledge.parse.docling_adapter import available; print(available())'
}
check "Docling's models are where Knowledge reads them" "$(docling_on)" True

SYNAPSE_PACKAGE=$B_WHEEL "$UV" run --no-project --quiet --python 3.12 python /b/synapse-install.pyz \
  upgrade --uv "$UV" --version "$B_VERSION" >/dev/null 2>&1 || fail "the upgrade failed"

read -r version torch docling nvidia <<<"$(parts)"
check "upgraded" "$version" "$B_VERSION"
check "the upgrade kept knowledge: torch is still the CPU build" "${torch##*+}" cpu
check "the upgrade kept knowledge: no CUDA libraries" "$nvidia" 0
[ "$docling" != missing ] || fail "the upgrade dropped docling-slim"
grep -q 'extras = \["knowledge"\]' "$TOOL/uv-receipt.toml" || fail "the upgrade's receipt lost the extra"
grep -q -- '--torch-backend cpu -c .*constraints-knowledge.txt' ~/.synapse/logs/install.log \
  || fail "the upgrade did not use the knowledge constraints with the CPU build"
"$PY" -c 'import docling' || fail "docling does not import"
check "Docling still on after the upgrade" "$(docling_on)" True
check "the models were fetched once, not again" "$(grep -c 'setup --knowledge-models' ~/.synapse/logs/install.log)" 2
grep -q '"models": "present"' ~/.synapse/logs/install.log || fail "the upgrade fetched the models again"
echo "OK: $version with knowledge, torch $torch"
AS_ME
