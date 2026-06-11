"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import { Key, Copy, CheckCircle, AlertCircle, BookOpen, X, ChevronDown, ChevronRight, Zap } from 'lucide-react';

interface ApiKeyRecord {
    id: string;
    name: string;
    key_prefix: string;
    created_at: string;
    last_used_at: string | null;
    is_active: boolean;
}

// ── Shared sub-components ─────────────────────────────────────────────────────

const CodeBlock = ({ code }: { code: string }) => {
    const [copied, setCopied] = useState(false);
    const copy = async () => {
        await navigator.clipboard.writeText(code);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };
    return (
        <div className="relative group">
            <pre className="bg-zinc-950 border border-zinc-800 p-3 text-xs text-zinc-400 overflow-x-auto font-mono leading-relaxed">
                {code}
            </pre>
            <button
                onClick={copy}
                className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity px-2 py-1 bg-zinc-800 hover:bg-zinc-700 text-zinc-400 hover:text-white text-[10px] font-bold border border-zinc-700"
            >
                {copied ? '✓ Copied' : 'Copy'}
            </button>
        </div>
    );
};

const Section = ({ title, children, defaultOpen = false }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) => {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="border border-zinc-800">
            <button
                onClick={() => setOpen(o => !o)}
                className="w-full flex items-center justify-between px-4 py-3 bg-zinc-900 hover:bg-zinc-800 transition-colors text-left"
            >
                <span className="text-xs uppercase font-bold text-zinc-300 tracking-wider">{title}</span>
                {open ? <ChevronDown className="w-3.5 h-3.5 text-zinc-500" /> : <ChevronRight className="w-3.5 h-3.5 text-zinc-500" />}
            </button>
            {open && <div className="p-4 space-y-4 border-t border-zinc-800">{children}</div>}
        </div>
    );
};

const EndpointRow = ({ method, path, desc, badge }: { method: string; path: string; desc: string; badge?: string }) => {
    const color = method === 'GET' ? 'text-emerald-400' : method === 'DELETE' ? 'text-red-400' : 'text-blue-400';
    return (
        <div className="flex items-start gap-3 py-1.5 border-b border-zinc-800/50 last:border-b-0">
            <span className={`text-[10px] font-bold uppercase w-9 shrink-0 pt-0.5 ${color}`}>{method}</span>
            <code className="text-xs font-mono text-zinc-300 shrink-0">{path}</code>
            <span className="text-xs text-zinc-600 flex-1">{desc}</span>
            {badge && <span className="text-[9px] font-bold px-1.5 py-0.5 bg-violet-900/40 text-violet-400 border border-violet-800/50 shrink-0">{badge}</span>}
        </div>
    );
};

// ── V1 Docs ───────────────────────────────────────────────────────────────────

const V1Docs = ({ BASE }: { BASE: string }) => (
    <>
        <Section title="Authentication" defaultOpen>
            <p className="text-xs text-zinc-600">All endpoints require a Bearer token in the Authorization header.</p>
            <CodeBlock code={`Authorization: Bearer sk-syn-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`} />
        </Section>

        <Section title="All Endpoints" defaultOpen>
            <div>
                <EndpointRow method="POST" path="/chat" desc="Synchronous chat — returns final response only" />
                <EndpointRow method="POST" path="/chat/stream" desc="Streaming chat via SSE — all events" />
                <EndpointRow method="GET"  path="/agents" desc="List all configured agents" />
                <EndpointRow method="GET"  path="/agents/{agent_id}" desc="Get agent details" />
                <EndpointRow method="GET"  path="/orchestrations" desc="List all orchestrations" />
                <EndpointRow method="GET"  path="/orchestrations/{id}" desc="Get orchestration details" />
                <EndpointRow method="POST" path="/orchestrations/{id}/run" desc="Start orchestration — sync" />
                <EndpointRow method="POST" path="/orchestrations/{id}/run/stream" desc="Start orchestration — SSE" />
                <EndpointRow method="POST" path="/orchestrations/runs/{run_id}/resume" desc="Resume after human step — sync" />
                <EndpointRow method="POST" path="/orchestrations/runs/{run_id}/resume/stream" desc="Resume after human step — SSE" />
            </div>
        </Section>

        <Section title="Chat" defaultOpen>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">POST /chat — single message</label>
                <CodeBlock code={`curl -X POST ${BASE}/chat \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"message": "Hello!", "agent": "AGENT_ID"}'

# Response:
# {
#   "response": "Hi! How can I help?",
#   "session_id": "api_abc123",
#   "agent_id": "AGENT_ID",
#   "agent_name": "My Agent"
# }`} />
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">POST /chat/stream — SSE</label>
                <CodeBlock code={`curl -N -X POST ${BASE}/chat/stream \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"message": "Explain RAG", "agent": "AGENT_ID"}'

# SSE events emitted in order:
# data: {"type": "session",  "session_id": "api_abc123", "agent_id": "..."}
# data: {"type": "thinking", "message": "..."}
# data: {"type": "response", "content": "...", "session_id": "api_abc123"}
# data: {"type": "done"}`} />
            </div>
        </Section>

        <Section title="Agents & Orchestrations">
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">GET /agents</label>
                <CodeBlock code={`curl ${BASE}/agents \\
  -H "Authorization: Bearer YOUR_API_KEY"

# [{"id":"agent_123","name":"My Agent","type":"conversational","model":"...","capabilities":[]}]`} />
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">GET /orchestrations</label>
                <CodeBlock code={`curl ${BASE}/orchestrations \\
  -H "Authorization: Bearer YOUR_API_KEY"

# [{"id":"orch_abc","name":"Weekly Report","description":"...","steps":4}]`} />
            </div>
        </Section>

        <Section title="Example — Multi-Turn Agent Conversation">
            <p className="text-xs text-zinc-600">
                The <code className="text-zinc-400">session_id</code> from each response links messages into the same conversation thread.
            </p>
            <CodeBlock code={`import requests

BASE = "${BASE}"
H = {"Authorization": "Bearer YOUR_API_KEY"}

# Turn 1 — start conversation
r1 = requests.post(f"{BASE}/chat", headers=H,
    json={"message": "Summarize Q3 revenue", "agent": "AGENT_ID"})
d1 = r1.json()
session_id = d1["session_id"]   # save this
print(d1["response"])

# Turn 2 — follow up in same session
r2 = requests.post(f"{BASE}/chat", headers=H,
    json={"message": "Now compare with Q2",
          "agent": "AGENT_ID",
          "session_id": session_id})  # pass it back
print(r2.json()["response"])`} />
        </Section>

        <Section title="Example — Orchestration Run & Human Resume">
            <p className="text-xs text-zinc-600">
                When an orchestration reaches a <strong className="text-zinc-400">Human Step</strong>, it pauses and returns <code className="text-zinc-400">status: paused</code> with a <code className="text-zinc-400">run_id</code>. Submit the human input to resume it.
            </p>
            <CodeBlock code={`import requests

BASE = "${BASE}"
H = {"Authorization": "Bearer YOUR_API_KEY"}

# Step 1 — start the orchestration
r = requests.post(f"{BASE}/orchestrations/ORCH_ID/run", headers=H,
    json={"message": "Run the approval workflow"})
data = r.json()

# Loop — handles multiple human steps
while data.get("status") == "paused":
    req = data["human_input_required"]
    print(f"\\nHuman input needed:\\n{req['prompt']}")

    # Collect input for each field (or a single string)
    fields = req.get("fields", [])
    if fields:
        user_input = {f: input(f"  {f}: ") for f in fields}
    else:
        user_input = input("  Your response: ")

    # Step 2 — resume with collected input
    run_id = data["run_id"]
    r = requests.post(f"{BASE}/orchestrations/runs/{run_id}/resume",
        headers=H, json={"response": user_input})
    data = r.json()

print(f"\\nCompleted: {data['response']}")`} />
        </Section>
    </>
);

// ── V2 Docs ───────────────────────────────────────────────────────────────────

const V2Docs = ({ BASE }: { BASE: string }) => (
    <>
        {/* Scale mode notice */}
        <div className="flex items-start gap-3 px-4 py-3 bg-violet-950/30 border border-violet-800/40 text-xs text-violet-300">
            <Zap className="w-3.5 h-3.5 text-violet-400 shrink-0 mt-0.5" />
            <span>
                V2 requires <strong className="text-violet-200">Scale mode</strong> enabled in Settings → Scale. All jobs are enqueued to Redis ARQ workers — the API returns immediately with a <code className="text-violet-300">run_id</code> and a <code className="text-violet-300">202 Accepted</code>.
            </span>
        </div>

        <Section title="Authentication" defaultOpen>
            <p className="text-xs text-zinc-600">All endpoints require a Bearer token in the Authorization header.</p>
            <CodeBlock code={`Authorization: Bearer sk-syn-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`} />
        </Section>

        <Section title="All V2 Endpoints" defaultOpen>
            <div>
                <EndpointRow method="POST" path="/orchestrations/{id}/run"              desc="Enqueue orchestration — returns run_id immediately"    badge="202" />
                <EndpointRow method="GET"  path="/orchestrations/runs/{run_id}/stream"  desc="SSE stream — all step events, supports Last-Event-ID"  />
                <EndpointRow method="GET"  path="/orchestrations/runs/{run_id}/status"  desc="Poll run status from Postgres"                         />
                <EndpointRow method="POST" path="/orchestrations/runs/{run_id}/cancel"  desc="Distributed cancel — worker stops at next step boundary"/>
                <EndpointRow method="POST" path="/orchestrations/runs/{run_id}/resume"  desc="Resume after human step — re-enqueues resume job"      badge="202" />
                <EndpointRow method="GET"  path="/orchestrations"                        desc="List all orchestration definitions from Postgres"       />
                <EndpointRow method="GET"  path="/orchestrations/{id}"                  desc="Get a single orchestration definition"                  />
                <EndpointRow method="POST" path="/chat"                                 desc="Enqueue agent chat turn — returns session_id"          badge="202" />
                <EndpointRow method="GET"  path="/chat/{session_id}/stream"             desc="SSE stream for a chat session"                         />
                <EndpointRow method="GET"  path="/chat/{session_id}/status"             desc="Poll chat session status and message history"          />
                <EndpointRow method="GET"  path="/agents"                               desc="List all agent definitions from Postgres"               />
                <EndpointRow method="GET"  path="/agents/{agent_id}"                    desc="Get a single agent definition"                         />
                <EndpointRow method="GET"  path="/orchestrations/runs/{run_id}/events" desc="Full event history for a run (Redis Stream replay)"     badge="new" />
                <EndpointRow method="GET"  path="/chat/{session_id}/events"            desc="Full event history for a chat session (Redis Stream)"   badge="new" />
                <EndpointRow method="GET"  path="/workers"                              desc="List registered workers and their health status"        />
                <EndpointRow method="GET"  path="/queue/stats"                          desc="Current queue depth and active job count"               />
                <EndpointRow method="GET"  path="/metrics"                              desc="Prometheus metrics endpoint"                            />
            </div>
        </Section>

        <Section title="Enqueue an Orchestration" defaultOpen>
            <p className="text-xs text-zinc-600">
                Returns <code className="text-zinc-400">202 Accepted</code> immediately. The <code className="text-zinc-400">run_id</code> is your handle for streaming events and polling status.
            </p>
            <CodeBlock code={`curl -s -X POST ${BASE}/orchestrations/ORCH_ID/run \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "message": "Write a competitive analysis of Redis vs Kafka",
    "tenant_id": "marketing-team",
    "webhook_url": "https://your-app.com/webhooks/synapse",
    "webhook_secret": "your-hmac-secret"
  }'

# 202 Response:
# {
#   "run_id": "run_2483e1773407_1780499750067",
#   "status": "queued",
#   "stream_url": "/api/v2/orchestrations/runs/run_.../stream",
#   "status_url": "/api/v2/orchestrations/runs/run_.../status"
# }`} />
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">Request body fields</label>
                <div className="text-xs font-mono space-y-1 text-zinc-500">
                    <div><span className="text-zinc-300">message</span>       <span className="ml-2 text-zinc-700">string — initial prompt sent to the orchestration</span></div>
                    <div><span className="text-zinc-300">tenant_id</span>     <span className="ml-2 text-zinc-700">string? — used for per-tenant quota and isolation</span></div>
                    <div><span className="text-zinc-300">webhook_url</span>   <span className="ml-2 text-zinc-700">string? — POST result here on completion/failure</span></div>
                    <div><span className="text-zinc-300">webhook_secret</span><span className="ml-2 text-zinc-700">string? — HMAC-SHA256 secret for X-Synapse-Signature</span></div>
                    <div><span className="text-zinc-300">session_id</span>    <span className="ml-2 text-zinc-700">string? — link run to a named session</span></div>
                    <div><span className="text-zinc-300">priority</span>      <span className="ml-2 text-zinc-700">int? — higher = picked up sooner (default 0)</span></div>
                </div>
            </div>
        </Section>

        <Section title="Stream Run Events (SSE)" defaultOpen>
            <p className="text-xs text-zinc-600">
                Subscribe to real-time step events. The stream stays open until <code className="text-zinc-400">{`{"type":"done"}`}</code> (run complete). If the run hits a Human Step it emits <code className="text-zinc-400">{`{"type":"paused"}`}</code> — the stream <strong className="text-zinc-400">stays open</strong> and resumes automatically after human input is submitted. Pass <code className="text-zinc-400">Last-Event-ID</code> to replay missed events on reconnect.
            </p>
            <CodeBlock code={`# Connect to the stream
curl -N "${BASE}/orchestrations/runs/RUN_ID/stream" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# Events arrive in order:
# id: 1780499750127-0
# data: {"type": "worker_picked_up", "worker_id": "worker-prod-01"}
#
# id: 1780499750218-0
# data: {"type": "step_start", "orch_step_id": "step_research", "step_name": "Research Topic", "step_type": "agent"}
#
# id: 1780499750229-0
# data: {"type": "tool_execution", "tool_name": "web_search", "args": {...}}
#
# id: 1780499758250-0
# data: {"type": "step_complete", "orch_step_id": "step_research", "duration_seconds": 8.02}
#
# ── Human Step: stream stays open, waiting for /resume ──────────────────
# id: 1780499758260-0
# data: {"type": "human_input_required", "orch_step_id": "step_approve", "prompt": "Approve?", "fields": ["decision"]}
#
# id: 1780499758261-0
# data: {"type": "paused"}      ← stream stays open, do NOT close
#
# ── After POST /resume — worker picks up and continues ──────────────────
# id: 1780499770100-0
# data: {"type": "worker_picked_up", "worker_id": "worker-prod-01"}
#
# id: 1780499775200-0
# data: {"type": "orchestration_complete", "status": "completed", "final_state": {...}}
#
# id: 1780499775210-0
# data: {"type": "done"}        ← close the stream here`} />
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">Sentinel event types</label>
                <div className="text-xs font-mono space-y-1 text-zinc-500">
                    <div><span className="text-zinc-300">{`{"type":"done"}`}</span>            <span className="ml-2 text-zinc-700">Run finished — close the connection</span></div>
                    <div><span className="text-zinc-300">{`{"type":"paused"}`}</span>          <span className="ml-2 text-zinc-700">Human step hit — keep connection open, more events after /resume</span></div>
                    <div><span className="text-zinc-300">{`{"type":"stream_complete"}`}</span> <span className="ml-2 text-zinc-700">Late reconnect after done — server closes gracefully, no new events</span></div>
                </div>
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">Reconnect — replay missed events</label>
                <CodeBlock code={`# Pass the last seen id: value to receive only missed events
curl -N "${BASE}/orchestrations/runs/RUN_ID/stream" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Last-Event-ID: 1780499750229-0"

# All events after 1780499750229-0 are returned, including paused and done.
# Events are stored in Redis for 1 hour — replay works across disconnects.`} />
            </div>
        </Section>

        <Section title="Get Event History">
            <p className="text-xs text-zinc-600">
                Retrieve the complete event log for a run or chat session as JSON. Useful for auditing, late subscribers, or building a replay UI. Events are stored in Redis Streams (1 hr TTL, up to 10 000 events per run).
            </p>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">GET /orchestrations/runs/{'{run_id}'}/events — full run history</label>
                <CodeBlock code={`curl -s "${BASE}/orchestrations/runs/RUN_ID/events" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# {
#   "run_id": "run_2483e1773407_1780499750067",
#   "count": 42,
#   "events": [
#     { "id": "1780499750127-0", "event": {"type": "worker_picked_up", ...} },
#     { "id": "1780499750218-0", "event": {"type": "step_start", ...} },
#     ...
#     { "id": "1780499775210-0", "event": {"type": "done"} }
#   ]
# }

# Paginate using Redis Stream IDs:
curl -s "${BASE}/orchestrations/runs/RUN_ID/events?start=1780499750218-0&end=+" \\
  -H "Authorization: Bearer YOUR_API_KEY"`} />
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">GET /chat/{'{session_id}'}/events — full chat history</label>
                <CodeBlock code={`curl -s "${BASE}/chat/sess_b639c740430d4a9a/events" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# {
#   "session_id": "sess_b639c740430d4a9a",
#   "count": 18,
#   "events": [
#     { "id": "1780499760100-0", "event": {"type": "thinking", ...} },
#     { "id": "1780499760200-0", "event": {"type": "tool_execution", ...} },
#     { "id": "1780499762000-0", "event": {"type": "final", "response": "..."} },
#     { "id": "1780499762010-0", "event": {"type": "done"} }
#   ]
# }`} />
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">Query params</label>
                <div className="text-xs font-mono space-y-1 text-zinc-500">
                    <div><span className="text-zinc-300">start</span> <span className="ml-2 text-zinc-700">Redis Stream ID — return events from this ID onwards (default <code className="text-zinc-500">-</code> = beginning)</span></div>
                    <div><span className="text-zinc-300">end</span>   <span className="ml-2 text-zinc-700">Redis Stream ID — return events up to this ID (default <code className="text-zinc-500">+</code> = latest)</span></div>
                </div>
            </div>
        </Section>

        <Section title="Poll Run Status">
            <CodeBlock code={`curl -s "${BASE}/orchestrations/runs/RUN_ID/status" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# While running:
# {
#   "run_id": "run_2483e1773407_...",
#   "orchestration_id": "orch_content_pipeline",
#   "status": "running",
#   "current_step_id": "step_write",
#   "waiting_for_human": false,
#   "worker_id": "worker-prod-01",
#   "total_cost_usd": 0.047,
#   "total_tokens_used": 12400,
#   "started_at": "2026-06-04T09:15:30Z",
#   "ended_at": null
# }
#
# Status values: queued → running → completed | cancelled | failed`} />
        </Section>

        <Section title="Cancel a Run">
            <p className="text-xs text-zinc-600">
                Publishes a cancellation signal to Redis. The executing worker checks it at each step boundary and stops cleanly.
            </p>
            <CodeBlock code={`curl -s -X POST "${BASE}/orchestrations/runs/RUN_ID/cancel" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# {"status": "cancellation_requested", "run_id": "run_..."}`} />
        </Section>

        <Section title="Resume After Human Step">
            <p className="text-xs text-zinc-600">
                When a run reaches a Human Step it pauses and emits <code className="text-zinc-400">{`{"type":"human_input_required"}`}</code> on the stream. Submit the response to resume.
            </p>
            <CodeBlock code={`curl -s -X POST "${BASE}/orchestrations/runs/RUN_ID/resume" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"response": "Approved — proceed with the report"}'

# 202 Response:
# {"run_id": "run_...", "status": "resuming", "stream_url": "..."}`} />
        </Section>

        <Section title="Agent Chat">
            <p className="text-xs text-zinc-600">
                Each chat turn is a queued job. The session history is persisted in Postgres, so any worker can handle any turn — no sticky sessions required.
            </p>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">POST /chat — enqueue a turn</label>
                <CodeBlock code={`curl -s -X POST ${BASE}/chat \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "message": "Summarize our Q1 sales data from the database",
    "agent": "data-analyst",
    "session_id": null
  }'

# 202 Response:
# {
#   "session_id": "sess_b639c740430d4a9a",
#   "status": "queued",
#   "stream_url": "/api/v2/chat/sess_.../stream",
#   "status_url": "/api/v2/chat/sess_.../status"
# }`} />
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">GET /chat/{'{session_id}'}/stream — SSE events</label>
                <CodeBlock code={`curl -N "${BASE}/chat/sess_b639c740430d4a9a/stream" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# data: {"type": "status", "message": "Processing your request..."}
# data: {"type": "thinking", "message": "Analyzing your request..."}
# data: {"type": "tool_execution", "tool_name": "run_sql_query", "args": {...}}
# data: {"type": "final", "response": "Q1 revenue was $4.2M, up 18% YoY..."}
# data: {"type": "done"}`} />
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">Follow-up turn — pass session_id back</label>
                <CodeBlock code={`curl -s -X POST ${BASE}/chat \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "message": "Now compare that with Q2",
    "agent": "data-analyst",
    "session_id": "sess_b639c740430d4a9a"
  }'`} />
            </div>
        </Section>

        <Section title="Workers & Queue">
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">GET /workers — list registered workers</label>
                <CodeBlock code={`curl -s "${BASE}/workers" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# [
#   {
#     "worker_id": "worker-prod-01",
#     "hostname": "prod-server-01",
#     "address": "http://10.0.0.5:9000",
#     "status": "online",
#     "active_jobs": 3,
#     "max_jobs": 10,
#     "last_heartbeat": "2026-06-04T09:20:00Z",
#     "mcp_disabled": []
#   }
# ]`} />
            </div>
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500 tracking-wider">GET /queue/stats — backlog and active count</label>
                <CodeBlock code={`curl -s "${BASE}/queue/stats" \\
  -H "Authorization: Bearer YOUR_API_KEY"

# {"queue_name": "synapse:orchestrations:default", "queued": 12, "active": 8}`} />
            </div>
        </Section>

        <Section title="Example — Python Async Client">
            <p className="text-xs text-zinc-600">
                Enqueue a run and consume the SSE stream asynchronously.
            </p>
            <CodeBlock code={`import asyncio, httpx, json

BASE = "${BASE}"
HEADERS = {"Authorization": "Bearer YOUR_API_KEY"}

async def run_and_stream(orch_id: str, message: str):
    async with httpx.AsyncClient() as client:
        # 1. Enqueue — returns immediately
        r = await client.post(
            f"{BASE}/orchestrations/{orch_id}/run",
            headers=HEADERS,
            json={"message": message, "tenant_id": "my-team"},
        )
        r.raise_for_status()
        run_id = r.json()["run_id"]
        print(f"Queued → {run_id}")

        # 2. Stream events until done
        async with client.stream(
            "GET",
            f"{BASE}/orchestrations/runs/{run_id}/stream",
            headers={**HEADERS, "Accept": "text/event-stream"},
            timeout=None,
        ) as stream:
            async for line in stream.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                t = event.get("type")
                if t == "step_start":
                    print(f"  → {event['step_name']} ({event['step_type']})")
                elif t == "tool_execution":
                    print(f"     tool: {event['tool_name']}")
                elif t == "human_input_required":
                    print(f"  ⏸ waiting for human input: {event.get('prompt')}")
                elif t == "paused":
                    print("  (stream stays open — submit /resume to continue)")
                elif t == "orchestration_complete":
                    print(f"  ✓ {event['status']}")
                elif t in ("done", "stream_complete"):
                    break

asyncio.run(run_and_stream("ORCH_ID", "Analyse our top 10 customers"))`} />
        </Section>

        <Section title="Example — TypeScript Streaming">
            <CodeBlock code={`const BASE = "${BASE}";
const TOKEN = "YOUR_API_KEY";

async function runOrchestration(orchId: string, message: string) {
  const res = await fetch(\`\${BASE}/orchestrations/\${orchId}/run\`, {
    method: "POST",
    headers: { Authorization: \`Bearer \${TOKEN}\`, "Content-Type": "application/json" },
    body: JSON.stringify({ message, tenant_id: "my-team" }),
  });
  const { run_id } = await res.json();
  return run_id;
}

async function* streamEvents(runId: string) {
  const res = await fetch(\`\${BASE}/orchestrations/runs/\${runId}/stream\`, {
    headers: { Authorization: \`Bearer \${TOKEN}\` },
  });
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\\n");
    buf = lines.pop()!;
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const ev = JSON.parse(line.slice(6));
        yield ev;
        if (ev.type === "done" || ev.type === "stream_complete") return;
      }
    }
  }
}

// Usage
const runId = await runOrchestration("ORCH_ID", "Write a market analysis");
for await (const event of streamEvents(runId)) {
  if (event.type === "step_start") console.log("Step:", event.step_name);
  if (event.type === "orchestration_complete") console.log("Done:", event.status);
}`} />
        </Section>

        <Section title="Example — React Hook (Live Step Feed)">
            <CodeBlock code={`import { useState, useEffect } from "react";

const BASE = "${BASE}";

function useOrchestrationRun(runId: string | null) {
  const [events, setEvents] = useState<any[]>([]);
  const [status, setStatus] = useState<"idle" | "running" | "done">("idle");

  useEffect(() => {
    if (!runId) return;
    setStatus("running");
    const es = new EventSource(
      \`\${BASE}/orchestrations/runs/\${runId}/stream?token=YOUR_API_KEY\`
    );
    es.onmessage = (e) => {
      const event = JSON.parse(e.data);
      setEvents((prev) => [...prev, event]);
      if (event.type === "done" || event.type === "stream_complete") { setStatus("done"); es.close(); }
    };
    return () => es.close();
  }, [runId]);

  return { events, status };
}

// In your component:
// const { events, status } = useOrchestrationRun(runId);
// events.filter(e => e.type === "step_start").map(e => <StepCard key={e.orch_step_id} {...e} />)`} />
        </Section>
    </>
);

// ── Docs Drawer ───────────────────────────────────────────────────────────────

const DocsDrawer = ({ open, onClose, port }: { open: boolean; onClose: () => void; port: string }) => {
    const [version, setVersion] = useState<'v1' | 'v2'>('v1');
    const BASE = `http://localhost:${port}/api/${version}`;

    useEffect(() => {
        const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        if (open) document.addEventListener('keydown', handler);
        return () => document.removeEventListener('keydown', handler);
    }, [open, onClose]);

    return (
        <>
            {/* Backdrop */}
            <div
                className={`fixed inset-0 z-40 bg-black/50 transition-opacity duration-300 ${open ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
                onClick={onClose}
            />
            {/* Drawer */}
            <div className={`fixed top-0 right-0 z-50 h-full w-full md:w-3/4 bg-zinc-950 border-l border-zinc-800 flex flex-col shadow-2xl transition-transform duration-300 ease-in-out ${open ? 'translate-x-0' : 'translate-x-full'}`}>
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-800 shrink-0">
                    <div className="flex items-center gap-3">
                        <BookOpen className="w-4 h-4 text-zinc-400" />
                        <h2 className="text-sm uppercase font-bold text-zinc-200 tracking-wider">API Reference</h2>
                        {/* Version toggle */}
                        <div className="flex items-center border border-zinc-700 overflow-hidden ml-2">
                            <button
                                onClick={() => setVersion('v1')}
                                className={`px-3 py-1 text-[10px] font-bold uppercase tracking-wider transition-colors ${version === 'v1' ? 'bg-zinc-200 text-zinc-900' : 'bg-transparent text-zinc-500 hover:text-zinc-300'}`}
                            >
                                V1
                            </button>
                            <button
                                onClick={() => setVersion('v2')}
                                className={`px-3 py-1 text-[10px] font-bold uppercase tracking-wider transition-colors flex items-center gap-1 ${version === 'v2' ? 'bg-violet-600 text-white' : 'bg-transparent text-zinc-500 hover:text-zinc-300'}`}
                            >
                                <Zap className="w-2.5 h-2.5" />
                                V2
                            </button>
                        </div>
                    </div>
                    <div className="flex items-center gap-4">
                        <code className="text-[10px] text-zinc-500 bg-zinc-900 border border-zinc-800 px-2 py-1 font-mono">Base: {BASE}</code>
                        <button onClick={onClose} className="text-zinc-500 hover:text-white transition-colors">
                            <X className="w-4 h-4" />
                        </button>
                    </div>
                </div>

                {/* Scrollable content */}
                <div className="flex-1 overflow-y-auto p-6 space-y-3 modern-scrollbar">
                    {version === 'v1' ? <V1Docs BASE={BASE} /> : <V2Docs BASE={BASE} />}
                </div>
            </div>
        </>
    );
};

// ── Main Tab ─────────────────────────────────────────────────────────────────

export const APIKeysTab = () => {
    const [keys, setKeys] = useState<ApiKeyRecord[]>([]);
    const [loading, setLoading] = useState(true);
    const [newKeyName, setNewKeyName] = useState('');
    const [generating, setGenerating] = useState(false);
    const [revealedKey, setRevealedKey] = useState<string | null>(null);
    const [copied, setCopied] = useState(false);
    const [toast, setToast] = useState<{ message: string; type: 'success' | 'error' } | null>(null);
    const [docsOpen, setDocsOpen] = useState(false);

    const backendPort = process.env.NEXT_PUBLIC_BACKEND_PORT || '8765';

    const showToast = (message: string, type: 'success' | 'error' = 'success') => {
        setToast({ message, type });
        setTimeout(() => setToast(null), 4000);
    };

    const fetchKeys = useCallback(async () => {
        try {
            const res = await fetch('/api/settings/api-keys');
            if (res.ok) setKeys(await res.json());
        } catch { /* ignore */ }
        finally { setLoading(false); }
    }, []);

    useEffect(() => { fetchKeys(); }, [fetchKeys]);

    const handleGenerate = async () => {
        if (!newKeyName.trim()) { showToast('Please enter a key name', 'error'); return; }
        setGenerating(true);
        try {
            const res = await fetch('/api/settings/api-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: newKeyName.trim() }),
            });
            if (res.ok) {
                const data = await res.json();
                setRevealedKey(data.key);
                setNewKeyName('');
                fetchKeys();
                showToast('API key generated!', 'success');
            } else {
                showToast('Failed to generate key', 'error');
            }
        } catch { showToast('Error generating key', 'error'); }
        finally { setGenerating(false); }
    };

    const handleDelete = async (id: string) => {
        try {
            const res = await fetch(`/api/settings/api-keys/${id}`, { method: 'DELETE' });
            if (res.ok) { setKeys(prev => prev.filter(k => k.id !== id)); showToast('Key deleted', 'success'); }
        } catch { showToast('Error deleting key', 'error'); }
    };

    const handleCopy = async (text: string) => {
        await navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };

    return (
        <div className="space-y-8">
            {/* Toast */}
            {toast && (
                <div className={`fixed top-6 right-6 z-50 flex items-center gap-2 px-4 py-3 text-sm font-medium shadow-lg
                    ${toast.type === 'success' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-red-500/20 text-red-400 border border-red-500/30'}`}>
                    {toast.type === 'success' ? <CheckCircle className="w-4 h-4" /> : <AlertCircle className="w-4 h-4" />}
                    {toast.message}
                </div>
            )}

            {/* Key Reveal Modal */}
            {revealedKey && (
                <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
                    <div className="bg-zinc-900 border border-zinc-700 p-6 max-w-lg w-full mx-4 shadow-2xl">
                        <div className="flex items-center gap-2 mb-4">
                            <Key className="w-5 h-5 text-amber-400" />
                            <h3 className="text-sm uppercase font-bold text-zinc-100 tracking-wider">Your API Key</h3>
                        </div>
                        <p className="text-xs text-zinc-500 mb-4">
                            Copy this key now — it will <strong className="text-zinc-300">never be shown again</strong>.
                        </p>
                        <div className="bg-zinc-950 border border-zinc-800 p-3 font-mono text-sm text-emerald-400 break-all mb-4">
                            {revealedKey}
                        </div>
                        <div className="flex gap-3">
                            <button onClick={() => handleCopy(revealedKey)}
                                className="flex items-center gap-2 px-4 py-2 bg-white text-black hover:bg-zinc-200 text-xs font-bold transition-colors">
                                {copied ? <CheckCircle className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                                {copied ? 'Copied!' : 'Copy Key'}
                            </button>
                            <button onClick={() => setRevealedKey(null)}
                                className="px-4 py-2 bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs font-bold transition-colors">
                                Done
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Docs Drawer */}
            <DocsDrawer open={docsOpen} onClose={() => setDocsOpen(false)} port={backendPort} />

            {/* Generate New Key */}
            <div className="space-y-2">
                <div className="flex items-center justify-between">
                    <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Generate API Key</label>
                    <button
                        onClick={() => setDocsOpen(true)}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-[10px] uppercase font-bold tracking-wider text-zinc-400 hover:text-white bg-zinc-900 border border-zinc-800 hover:border-zinc-600 transition-colors"
                    >
                        <BookOpen className="w-3 h-3" />
                        View Docs
                    </button>
                </div>
                <p className="text-xs text-zinc-600">
                    API keys authenticate calls to <code className="bg-zinc-900 border border-zinc-800 px-1 py-0.5 text-zinc-400">/api/v1/*</code> (synchronous) and <code className="bg-zinc-900 border border-zinc-800 px-1 py-0.5 text-zinc-400">/api/v2/*</code> (distributed, scale mode).
                </p>
                <div className="flex gap-2">
                    <input
                        type="text"
                        value={newKeyName}
                        onChange={e => setNewKeyName(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && handleGenerate()}
                        placeholder="Key name (e.g., Slack Bot, Internal Tool)"
                        className="flex-1 bg-zinc-900 border border-zinc-800 p-2.5 text-sm focus:border-white focus:outline-none transition-colors text-white placeholder:text-zinc-700 font-medium"
                    />
                    <button
                        onClick={handleGenerate}
                        disabled={generating}
                        className="px-4 py-2.5 text-xs font-bold bg-white text-black hover:bg-zinc-200 transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        {generating ? 'Generating…' : 'Generate'}
                    </button>
                </div>
            </div>

            {/* Keys List */}
            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Active Keys</label>
                {loading ? (
                    <p className="text-xs text-zinc-600 py-4">Loading…</p>
                ) : keys.length === 0 ? (
                    <p className="text-xs text-zinc-600 py-4">No API keys yet. Generate one above to get started.</p>
                ) : (
                    <div className="space-y-1">
                        {keys.map(k => (
                            <div key={k.id} className="flex items-center justify-between bg-zinc-900 border border-zinc-800 px-3 py-2 group">
                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center gap-3">
                                        <span className="text-sm font-medium text-zinc-200">{k.name}</span>
                                        <code className="text-xs bg-zinc-950 border border-zinc-800 text-zinc-500 px-1.5 py-0.5 font-mono">
                                            {k.key_prefix}…
                                        </code>
                                    </div>
                                    <div className="flex items-center gap-4 text-[10px] text-zinc-600 mt-0.5">
                                        <span>Created: {new Date(k.created_at).toLocaleDateString()}</span>
                                        <span>Last used: {k.last_used_at ? new Date(k.last_used_at).toLocaleDateString() : 'Never'}</span>
                                    </div>
                                </div>
                                <button
                                    onClick={() => handleDelete(k.id)}
                                    className="text-zinc-600 hover:text-red-400 transition-colors text-xs ml-2 flex-shrink-0 opacity-0 group-hover:opacity-100"
                                >
                                    Remove
                                </button>
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
};
