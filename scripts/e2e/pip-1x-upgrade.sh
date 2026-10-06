#!/bin/bash
# A pip install of 1.9.1, set up by its own wizard, upgraded with
# `pip install --upgrade` — inside a clean container. Two users, two ways in:
#
#   me:  the first `synapse start` at a terminal. It brings the 1.x data across
#        and asks nothing 1.x already answered.
#   me2: `synapse setup` first, then `synapse start`. The new answers stay; the
#        1.x file fills in only what they left out.
#
#   docker run --rm -i -v "$DIST:/w:ro" ubuntu:24.04 bash /w/pip-1x-upgrade.sh
#
# $DIST holds the 2.x wheel (stamped above 1.9.1). Node 20.9+ comes from /node
# when mounted, else from nodejs.org. Before IU1 the start asked every question
# again, then the backend's boot import put every answer back to its 1.x value.
set -u
apt-get update -qq >/dev/null && apt-get install -y -qq curl ca-certificates python3 python3-venv \
  sqlite3 procps xz-utils >/dev/null 2>&1
if [ ! -x /node/bin/node ]; then
  mkdir -p /opt/node && curl -fsSL https://nodejs.org/dist/v22.11.0/node-v22.11.0-linux-x64.tar.xz \
    | tar -xJ -C /opt/node --strip-components=1
  NODE=/opt/node
else
  NODE=/node
fi
mkdir -p /cache && chmod 777 /cache
export WHEEL=$(ls /w/*.whl | head -1) NODE

as_user() {
  useradd -m "$1"
  su "$1" --preserve-environment -c 'bash -s' <<'AS_USER'
set -u
HOME=/home/$(whoami)
cd ~
export PATH=~/v/bin:$NODE/bin:$PATH PIP_CACHE_DIR=/cache/pip PLAYWRIGHT_DOWNLOAD_HOST=http://127.0.0.1:9
fail() { echo "FAIL ($(whoami)): $*"; tail -n 30 ~/.synapse/logs/*.log 2>/dev/null; exit 1; }
check() { [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"; echo "ok  $(whoami): $1"; }
setting() { sqlite3 ~/.synapse/var/synapse.db "select value from scale_settings where key='$1'"; }

python3 -m venv ~/v && pip install -q synapse-orch-ai==1.9.1 >~/pip1.log 2>&1 || fail "1.9.1 did not install"
SITE=$(python3 -c 'import site; print(site.getsitepackages()[0])')
# 1.9.1's own wizard: agent OldName, OpenAI, key sk-OLD, model gpt-4o, default ports.
printf 'OldName\n3\nsk-OLD\ngpt-4o\n\n\n' | synapse setup >/dev/null 2>&1
KEY=$(synapse api-keys generate "old app" 2>&1 | grep -oE 'sk-syn-[A-Za-z0-9_-]+' | tail -1)
[ -n "$KEY" ] || fail "1.9.1 made no API key"
echo '[{"id":"agent_a1","name":"Researcher","model":"gpt-4o"}]' > ~/.synapse/data/user_agents.json
mkdir -p "$SITE/backend/data/chat_sessions"
echo '{"session_id":"s1","agent_id":"agent_a1","turns":[{"user":"hi","assistant":"hello"}],"last_updated":"2026-08-01T00:00:00Z"}' \
  > "$SITE/backend/data/chat_sessions/agent_a1_s1.json"

pip install -q --upgrade "$WHEEL" >~/pip2.log 2>&1 || fail "pip could not upgrade to 2.x"

if [ "$(whoami)" = me ]; then
  # A terminal (script gives one) with answers ready, as if someone typed them.
  printf 'NewName\n4\nsk-ant-NEW\nclaude-new\n\n\n' | timeout 600 script -qec 'synapse start --detach --no-browser' /dev/null >~/start.txt 2>&1
  check "no question asked" "$(grep -c 'Interactive Setup' ~/start.txt)" 0
  grep -q "Brought your Synapse 1.x data across" ~/start.txt || fail "the start did not say it brought the data across"
  EXPECT_MODEL='"gpt-4o"' EXPECT_NAME='"OldName"' EXPECT_ANTHROPIC=""
else
  printf 'NewName\n4\nsk-ant-NEW\nclaude-new\n\n\n' | synapse setup >/dev/null 2>&1
  synapse start --detach --no-browser </dev/null >~/start.txt 2>&1
  EXPECT_MODEL='"claude-new"' EXPECT_NAME='"NewName"' EXPECT_ANTHROPIC='"sk-ant-NEW"'
fi
for i in $(seq 1 120); do synapse status --json 2>/dev/null | tail -1 | grep -q '"ready": true' && break; sleep 2; done
synapse status --json 2>/dev/null | tail -1 | grep -q '"ready": true' || fail "not running: $(tail -5 ~/start.txt)"

check "model" "$(setting model)" "$EXPECT_MODEL"
check "agent name" "$(setting agent_name)" "$EXPECT_NAME"
check "1.x OpenAI key kept" "$(setting openai_key)" '"sk-OLD"'
[ -z "$EXPECT_ANTHROPIC" ] || check "new Anthropic key kept" "$(setting anthropic_key)" "$EXPECT_ANTHROPIC"
check "agents" "$(sqlite3 ~/.synapse/var/synapse.db "select id from agents where id='agent_a1'")" agent_a1
check "chats" "$(sqlite3 ~/.synapse/var/synapse.db 'select count(*) from chat_sessions')" 1
PORT=$(setting backend_port); PORT=${PORT:-8765}
check "the 1.x API key" "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $KEY" http://127.0.0.1:$PORT/api/v1/agents)" 200
synapse stop >/dev/null 2>&1
echo "OK ($(whoami))"
AS_USER
}

as_user me || exit 1
as_user me2 || exit 1
