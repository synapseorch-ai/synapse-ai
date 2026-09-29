#!/bin/bash
# A v1.9.1 script install, with data and a running backend, upgraded by v1.9.1's
# own `synapse upgrade` — inside a clean container.
#
#   git archive --prefix=SynapseAI/ v1.9.1 -o "$L/v191.tar.gz"
#   git archive --prefix=synapse-ai-2.0.0/ HEAD -o "$L/v2.tar.gz"
#   docker run --rm -i -v "$L:/l:ro" -v "$DIST:/w:ro" ubuntu:24.04 bash /l/legacy-upgrade.sh
#
# v1.9.1 downloads the release's source over its own folder and re-runs
# `[its venv python] synapse/cli.py upgrade` (v1.9.1 cli.py:1199-1208). This
# builds that install's real layout (the .installed marker, a venv holding the
# source's .pth, the .env naming backend/data, the PATH line), applies the v2
# source with v1.9.1's own `_download_and_apply_release`, runs its re-exec, and
# checks everything arrived and the old install is gone.
set -u
apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates python3 python3-venv procps sqlite3 >/dev/null 2>&1
useradd -m me
mkdir -p /cache && chown me /cache
export WHEEL=$(ls /w/*.whl | head -1)

su me --preserve-environment -c 'bash -s' <<'AS_ME'
set -u
HOME=/home/me
cd ~
ROOT=~/.local/share/SynapseAI
mkdir -p ~/.local/share && tar -xzf /l/v191.tar.gz -C ~/.local/share
cd "$ROOT"
python3 -m venv backend/venv
echo "$ROOT" > "$(backend/venv/bin/python -c 'import site;print(site.getsitepackages()[0])')/synapse-source.pth"
echo '{"installed_at": "2026-08-09T17:32:58Z"}' > .installed
printf 'SYNAPSE_DATA_DIR=backend/data\nSYNAPSE_INTERNAL_TOKEN=old-internal\nSYNAPSE_JWT_SECRET=old-jwt\n' > .env
printf '\n# Synapse AI\nexport PATH="%s/bin:$PATH"\n' "$ROOT" >> ~/.bashrc

D=backend/data
mkdir -p $D/chat_sessions $D/vault/tool_outputs $D/datasets backend/logs/orchestration_logs
echo '[{"id":"agent_a1","name":"Researcher","model":"gpt-4o"},{"id":"agent_a2","name":"Writer"}]' > $D/user_agents.json
echo '[{"id":"orch_o1","name":"Nightly report","entry_step_id":"s1","steps":[{"id":"s1","type":"print"}]}]' > $D/orchestrations.json
echo '[{"id":"sched_1","name":"Nightly","enabled":false,"target_type":"agent","target_id":"agent_a1","prompt":"go","schedule_type":"interval","interval_value":5,"interval_unit":"minutes"}]' > $D/schedules.json
echo '{"model":"claude-sonnet-4-6","anthropic_key":"sk-ant-test","agent_name":"Mine"}' > $D/settings.json
for i in 1 2; do echo "{\"session_id\":\"s$i\",\"agent_id\":\"agent_a1\",\"turns\":[{\"user\":\"hi $i\",\"assistant\":\"hello\"}],\"last_updated\":\"2026-08-01T00:00:00Z\"}" > $D/chat_sessions/agent_a1_s$i.json; done
echo "# saved by an agent" > $D/vault/tool_outputs/page.md
echo '{"q":1}' > $D/datasets/qa.jsonl
echo "run log" > backend/logs/orchestration_logs/run_1.log

# A 1.9 backend "running": a process whose command line is Synapse's.
(exec -a "python main.py" sleep 3000) &
echo $! > $D/backend.pid

backend/venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, os.path.expanduser("~/.local/share/SynapseAI"))
from synapse import cli
assert cli._download_and_apply_release("file:///l/v2.tar.gz"), "apply failed"
PY
set -a; . ./.env; set +a
SYNAPSE_UPGRADE_SKIP_DOWNLOAD=1 UV_CACHE_DIR=/cache SYNAPSE_PACKAGE="$WHEEL" \
  backend/venv/bin/python "$ROOT/synapse/cli.py" upgrade </dev/null
echo "UPGRADE_EXIT=$?"

cd ~
fail() { echo "FAIL: $*"; tail -n 40 ~/.synapse/logs/*.log; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"; echo "ok  $1"; }
DB=~/.synapse/var/synapse.db
check "old install removed" "$(test -e ~/.local/share/SynapseAI && echo yes || echo no)" no
check "old backend stopped" "$(pgrep -f 'python main.py' >/dev/null && echo yes || echo no)" no
check "PATH line removed" "$(grep -c SynapseAI ~/.bashrc)" 0
check "a backup" "$(ls ~/.synapse/backups/*.tar.gz | wc -l)" 1
check "jwt secret kept" "$(grep -c 'SYNAPSE_JWT_SECRET=old-jwt' ~/.synapse/.env)" 1
check "agents" "$(sqlite3 $DB "select id from agents where id like 'agent_a%' order by id" | tr '\n' ' ')" "agent_a1 agent_a2 "
check "orchestrations" "$(sqlite3 $DB "select id from orchestrations where id = 'orch_o1'")" orch_o1
check "schedules" "$(sqlite3 $DB 'select id from schedules')" sched_1
check "chats" "$(sqlite3 $DB 'select count(*) from chat_sessions')" 2
check "model" "$(sqlite3 $DB "select value from scale_settings where key='model'")" '"claude-sonnet-4-6"'
check "vault file" "$(cat ~/.synapse/var/blobs/default/vault/tool_outputs/page.md)" "# saved by an agent"
check "dataset" "$(cat ~/.synapse/var/blobs/default/datasets/qa.jsonl)" '{"q":1}'
PY=$(sed -n 's/.*"tool_python": "\([^"]*\)".*/\1/p' ~/.synapse/install.json)
STATUS=$("$PY" -m synapse status --json | tail -1)
LINK=$("$PY" -c 'import json,sys; s=json.loads(sys.argv[1]); assert s["ready"], s; print(s["link"])' "$STATUS") \
  || fail "not running after the upgrade: $STATUS"
echo "OK: running at $LINK"
AS_ME
