#!/bin/bash
# `synapse upgrade --with knowledge` on a pip install — inside a clean container.
#
#   docker run --rm -i -v "$DIST:/w:ro" ubuntu:24.04 bash /w/pip-with-knowledge.sh
#
# $DIST holds the 2.x wheel. PIP_FIND_LINKS points pip at it, so the same
# version installs again with the extra before it is on PyPI. A pip install
# has no uv receipt: the extra is named from what is installed, PyTorch comes
# from its CPU index, and Docling's models land where Knowledge reads them.
set -u
apt-get update -qq >/dev/null && apt-get install -y -qq ca-certificates python3 python3-venv >/dev/null 2>&1
useradd -m me
mkdir -p /cache && chmod 777 /cache
export WHEEL=$(ls /w/*.whl | head -1)

su me --preserve-environment -c 'bash -s' <<'AS_ME'
set -u
HOME=/home/me
cd ~
export PATH=~/v/bin:$PATH PIP_CACHE_DIR=/cache/pip PIP_FIND_LINKS=/w
fail() { echo "FAIL: $*"; tail -n 30 ~/.synapse/logs/install.log 2>/dev/null; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"; echo "ok  $1"; }

python3 -m venv ~/v && pip install -q "$WHEEL" >/tmp/pip.log 2>&1 || fail "the wheel did not install"
VERSION=$(synapse --version | awk '{print $NF}')
synapse upgrade --with knowledge </dev/null >~/up.txt 2>&1 || fail "the upgrade failed: $(tail -5 ~/up.txt)"

read -r installed torch nvidia <<<"$(python3 - <<'PYCODE'
import importlib.metadata as m
def v(d):
    try: return m.version(d)
    except m.PackageNotFoundError: return "missing"
nvidia = sum(1 for d in m.distributions() if (d.metadata["Name"] or "").lower().startswith("nvidia"))
print(v("synapse-orch-ai"), v("torch"), nvidia)
PYCODE
)"
check "the same version" "$installed" "$VERSION"
check "torch is the CPU build" "${torch##*+}" cpu
check "no CUDA libraries" "$nvidia" 0
check "Docling's models are where Knowledge reads them" "$(SYNAPSE_HOME=~/.synapse python3 -P -c 'from synapse import _store; _store.prepare()
from core.knowledge.parse.docling_adapter import available; print(available())')" True
check "asked again: up to date" "$(synapse upgrade --with knowledge </dev/null 2>&1 | tail -1)" \
  "Synapse $VERSION is up to date, with knowledge."
echo "OK: $VERSION with knowledge on pip, torch $torch"
AS_ME
