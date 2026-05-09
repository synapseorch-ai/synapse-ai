"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import { Key, Copy, CheckCircle, AlertCircle, BookOpen, X, ChevronDown, ChevronRight } from 'lucide-react';

interface ApiKeyRecord {
    id: string;
    name: string;
    key_prefix: string;
    created_at: string;
    last_used_at: string | null;
    is_active: boolean;
}

// ── Docs Drawer ──────────────────────────────────────────────────────────────

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

const EndpointRow = ({ method, path, desc }: { method: string; path: string; desc: string }) => {
    const color = method === 'GET' ? 'text-emerald-400' : 'text-blue-400';
    return (
        <div className="flex items-start gap-3 py-1.5 border-b border-zinc-800/50 last:border-b-0">
            <span className={`text-[10px] font-bold uppercase w-9 shrink-0 pt-0.5 ${color}`}>{method}</span>
            <code className="text-xs font-mono text-zinc-300 shrink-0">{path}</code>
            <span className="text-xs text-zinc-600">{desc}</span>
        </div>
    );
};

const DocsDrawer = ({ open, onClose, port }: { open: boolean; onClose: () => void; port: string }) => {
    const BASE = `http://localhost:${port}/api/v1`;

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
                    <div className="flex items-center gap-2">
                        <BookOpen className="w-4 h-4 text-zinc-400" />
                        <h2 className="text-sm uppercase font-bold text-zinc-200 tracking-wider">API Reference</h2>
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

                    {/* Auth */}
                    <Section title="Authentication" defaultOpen>
                        <p className="text-xs text-zinc-600">All endpoints require a Bearer token in the Authorization header.</p>
                        <CodeBlock code={`Authorization: Bearer sk-syn-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`} />
                    </Section>

                    {/* Endpoint index */}
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

                    {/* Chat endpoints */}
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

                    {/* Discovery */}
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

                    {/* Example: multi-turn conversation */}
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

                    {/* Example: orchestration run + resume */}
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
                    API keys allow external apps to interact with your agents and orchestrations via <code className="bg-zinc-900 border border-zinc-800 px-1 py-0.5 text-zinc-400">/api/v1/*</code> endpoints.
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
