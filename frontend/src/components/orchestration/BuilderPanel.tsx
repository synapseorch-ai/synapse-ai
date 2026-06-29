'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useRef, useEffect } from 'react';
import { X, Send, Sparkles, ChevronDown, ChevronUp, Loader2, Bot, Plus, CheckCircle2, ArrowRight, ChevronsRight } from 'lucide-react';
import type { Orchestration } from '@/types/orchestration';
import { renderTextContent } from '@/lib/utils';
import { readWithStallTimeout } from '@/lib/sse';

// ─── Types ────────────────────────────────────────────────────────────────────

interface ChatMessage {
    role: 'user' | 'assistant';
    content: string;
    kind?: 'text' | 'banner_orch' | 'banner_agent' | 'step_progress' | 'human_prompt' | 'tool_call' | 'tool_result' | 'routing';
    bannerText?: string;
    orchestration?: Orchestration;
    stepName?: string;
    stepStatus?: 'running' | 'complete';
    toolName?: string;
    toolArgs?: Record<string, any>;
    toolPreview?: string;
    routingDecision?: string;
}

interface BuilderPanelProps {
    isOpen: boolean;
    onClose: () => void;
    agents: any[];
    availableModels: string[];
    currentOrchestrationId: string | null;
    onOrchestrationSaved: (orch: Orchestration) => void;
    onAgentSaved: (agent: any) => void;
    sessionKey?: number;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function isActivity(msg: ChatMessage): boolean {
    return msg.kind === 'step_progress' || msg.kind === 'tool_call' || msg.kind === 'tool_result' || msg.kind === 'routing';
}

interface ActivityGroupProps {
    events: ChatMessage[];
    isLive: boolean;
}

function ActivityGroup({ events, isLive }: ActivityGroupProps) {
    const [expanded, setExpanded] = useState(false);
    const earlier = events.slice(0, -1);
    const latest = events[events.length - 1];

    const renderEvent = (msg: ChatMessage, i: number) => {
        if (msg.kind === 'step_progress') {
            const done = msg.stepStatus === 'complete';
            return (
                <div key={i} className="flex items-center gap-2 pl-1 py-0.5">
                    {done ? (
                        <CheckCircle2 size={12} className="text-emerald-500 shrink-0" />
                    ) : (
                        <Loader2 size={12} className="animate-spin text-purple-400 shrink-0" />
                    )}
                    <span className={`text-[11px] font-mono truncate ${done ? 'text-zinc-600' : 'text-zinc-400'}`}>
                        {msg.stepName}
                    </span>
                </div>
            );
        }
        if (msg.kind === 'tool_call') {
            return (
                <div key={i} className="flex items-start gap-2 pl-1 py-0.5">
                    <span className="text-violet-400 text-[10px] shrink-0 mt-0.5">🔧</span>
                    <details className="flex-1 min-w-0">
                        <summary className="text-[11px] text-violet-400 cursor-pointer list-none font-mono truncate">
                            {msg.toolName?.replace(/_/g, ' ')}
                        </summary>
                        <pre className="bg-zinc-800/60 rounded p-1.5 mt-0.5 text-[10px] text-zinc-300 overflow-x-auto whitespace-pre-wrap max-h-28 border border-zinc-700/40">
                            {JSON.stringify(msg.toolArgs, null, 2)}
                        </pre>
                    </details>
                </div>
            );
        }
        if (msg.kind === 'tool_result') {
            const preview = (msg.toolPreview || '').slice(0, 180);
            const truncated = (msg.toolPreview || '').length > 180;
            return (
                <div key={i} className="pl-7 text-[10px] text-zinc-600 py-0.5 font-mono truncate">
                    ↳ {preview}{truncated ? '…' : ''}
                </div>
            );
        }
        if (msg.kind === 'routing') {
            if (!msg.routingDecision) return null;
            return (
                <div key={i} className="flex items-center gap-1.5 pl-1 py-0.5">
                    <span className="text-[10px] text-zinc-600">→</span>
                    <span className="text-[10px] font-mono text-amber-500">{msg.routingDecision}</span>
                </div>
            );
        }
        return null;
    };

    return (
        <div className="space-y-0.5">
            {(isLive || expanded) && earlier.map((msg, i) => renderEvent(msg, i))}
            {!isLive && earlier.length > 0 && (
                <button
                    onClick={() => setExpanded(v => !v)}
                    className="flex items-center gap-1 pl-1 text-[10px] text-zinc-600 hover:text-zinc-400 transition-colors"
                >
                    {expanded
                        ? <><ChevronUp size={10} /> hide</>
                        : <><ChevronDown size={10} /> {earlier.length} earlier step{earlier.length !== 1 ? 's' : ''}</>}
                </button>
            )}
            {latest && renderEvent(latest, earlier.length)}
        </div>
    );
}

const TYPE_COLORS: Record<string, string> = {
    code: 'bg-sky-900/60 text-sky-400 border-sky-700/40',
    conversational: 'bg-violet-900/60 text-violet-400 border-violet-700/40',
    orchestrator: 'bg-amber-900/60 text-amber-400 border-amber-700/40',
    builder: 'bg-purple-900/60 text-purple-400 border-purple-700/40',
};

function typeColor(type: string) {
    return TYPE_COLORS[type] ?? 'bg-zinc-800 text-zinc-400 border-zinc-700/40';
}

const COLLAPSE_THRESHOLD = 600;

function HumanPromptCard({ content }: { content: string }) {
    const [expanded, setExpanded] = useState(false);
    const isLong = content.length > COLLAPSE_THRESHOLD;
    const visible = isLong && !expanded ? content.slice(0, COLLAPSE_THRESHOLD) + '…' : content;

    return (
        <div className="flex justify-start">
            <div className="w-full max-w-[95%] rounded-lg border border-amber-700/40 bg-zinc-900/60 overflow-hidden">
                <div className="flex items-center gap-1.5 px-3 py-1.5 border-b border-amber-700/30 bg-amber-900/10">
                    <span className="text-[9px] font-mono text-amber-500/70 uppercase tracking-widest">Evaluator</span>
                </div>
                <div className="px-3 py-2.5 text-zinc-300 text-[12px] leading-relaxed">
                    {renderTextContent(visible)}
                </div>
                {isLong && (
                    <button
                        onClick={() => setExpanded((v) => !v)}
                        className="w-full text-[10px] text-zinc-500 hover:text-zinc-300 py-1.5 border-t border-zinc-800/60 transition-colors flex items-center justify-center gap-1"
                    >
                        {expanded ? <><ChevronUp size={10} /> Show less</> : <><ChevronDown size={10} /> Show full plan</>}
                    </button>
                )}
            </div>
        </div>
    );
}

function markLastRunningComplete(msgs: ChatMessage[]): ChatMessage[] {
    const next = [...msgs];
    for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].kind === 'step_progress' && next[i].stepStatus === 'running') {
            next[i] = { ...next[i], stepStatus: 'complete' };
            break;
        }
    }
    return next;
}

// ─── Component ────────────────────────────────────────────────────────────────

const BUILDER_WELCOME_MESSAGE: ChatMessage = {
    role: 'assistant',
    content:
        "Hi! I'm the Synapse Builder. Tell me what kind of agent or orchestration you'd like to build, and I'll ask any clarifying questions before creating it.\n\nFor example:\n- *\"Build a research workflow that searches the web and writes a summary to the vault\"*\n- *\"Create an agent that monitors my emails and drafts replies\"*",
    kind: 'text',
};

export function BuilderPanel({
    isOpen,
    onClose,
    agents,
    availableModels,
    currentOrchestrationId,
    onOrchestrationSaved,
    onAgentSaved,
    sessionKey,
}: BuilderPanelProps) {
    const [messages, setMessages] = useState<ChatMessage[]>([BUILDER_WELCOME_MESSAGE]);
    const [selectedAgentIds, setSelectedAgentIds] = useState<Set<string>>(new Set());
    const [canCreateAgents, setCanCreateAgents] = useState(true);
    const [selectedModel, setSelectedModel] = useState('');
    const [input, setInput] = useState('');
    const [streaming, setStreaming] = useState(false);
    const [streamingStatus, setStreamingStatus] = useState<string | null>(null);
    const [accordionOpen, setAccordionOpen] = useState(false);
    const [pendingRun, setPendingRun] = useState<{ run_id: string; field: string } | null>(null);
    const [collapsed, setCollapsed] = useState(false);

    const bottomRef = useRef<HTMLDivElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);

    useEffect(() => {
        if (isOpen) setCollapsed(false);
    }, [isOpen]);

    useEffect(() => {
        if (sessionKey === undefined) return;
        setMessages([BUILDER_WELCOME_MESSAGE]);
        setSelectedAgentIds(new Set());
        setCanCreateAgents(true);
        setInput('');
        setAccordionOpen(false);
        setPendingRun(null);
    }, [sessionKey]);

    useEffect(() => {
        bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    const buildHistory = (msgs: ChatMessage[]) =>
        msgs
            .filter((m) => m.kind === 'text' || m.kind === undefined)
            .map((m) => ({ role: m.role, content: m.content }));

    const toggleAgent = (id: string) => {
        setSelectedAgentIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const sendMessage = async () => {
        const text = input.trim();
        if (!text || streaming) return;

        const userMsg: ChatMessage = { role: 'user', content: text, kind: 'text' };
        const historySnapshot = buildHistory(messages);
        setMessages((prev) => [...prev, userMsg]);
        setInput('');
        setStreaming(true);
        setStreamingStatus('Thinking…');

        const resumeCtx = pendingRun;
        if (resumeCtx) setPendingRun(null);

        const controller = new AbortController();

        try {
            const res = resumeCtx
                ? await fetch('/api/builder/resume', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        run_id: resumeCtx.run_id,
                        response: { [resumeCtx.field]: text },
                        model: selectedModel || undefined,
                    }),
                    signal: controller.signal,
                })
                : await fetch('/api/builder/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message: text,
                        history: historySnapshot,
                        selected_agent_ids: Array.from(selectedAgentIds),
                        can_create_agents: canCreateAgents,
                        model: selectedModel || undefined,
                        current_orchestration_id: currentOrchestrationId,
                    }),
                    signal: controller.signal,
                });

            if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await readWithStallTimeout(reader, controller);
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    let event: any;
                    try { event = JSON.parse(line.slice(6)); } catch { continue; }

                    switch (event.type) {
                        case 'thinking': {
                            const msg = (event.message as string) || '';
                            if (msg.startsWith('Running: ')) {
                                const stepName = msg.replace('Running: ', '');
                                setMessages((prev) => {
                                    const completed = markLastRunningComplete(prev);
                                    return [...completed, {
                                        role: 'assistant',
                                        kind: 'step_progress',
                                        content: '',
                                        stepName,
                                        stepStatus: 'running',
                                    }];
                                });
                                setStreamingStatus(stepName);
                            } else {
                                setStreamingStatus('Processing…');
                            }
                            break;
                        }

                        case 'tool_call':
                            setMessages((prev) => [...prev, {
                                role: 'assistant',
                                kind: 'tool_call',
                                content: '',
                                toolName: event.tool_name as string,
                                toolArgs: (event.args || {}) as Record<string, any>,
                            }]);
                            setStreamingStatus(`🔧 ${(event.tool_name as string).replace(/_/g, ' ')}…`);
                            break;

                        case 'tool_result':
                            setMessages((prev) => [...prev, {
                                role: 'assistant',
                                kind: 'tool_result',
                                content: '',
                                toolName: event.tool_name as string,
                                toolPreview: (event.result || event.preview || '') as string,
                            }]);
                            setStreamingStatus(`✓ ${((event.tool_name as string | undefined) ?? 'tool').replace(/_/g, ' ')}`);
                            break;

                        case 'step_complete':
                            setMessages((prev) => markLastRunningComplete(prev));
                            break;

                        case 'routing_decision':
                            setMessages((prev) => [...prev, {
                                role: 'assistant',
                                kind: 'routing',
                                content: '',
                                routingDecision: event.decision as string,
                            }]);
                            break;

                        case 'orchestration_saved':
                            onOrchestrationSaved(event.orchestration as Orchestration);
                            setMessages((prev) => [
                                ...markLastRunningComplete(prev),
                                {
                                    role: 'assistant',
                                    kind: 'banner_orch',
                                    content: '',
                                    bannerText: event.orchestration?.name ?? event.orchestration?.id,
                                    orchestration: event.orchestration,
                                },
                            ]);
                            break;

                        case 'agent_saved':
                            onAgentSaved(event.agent);
                            setMessages((prev) => [...prev, {
                                role: 'assistant', kind: 'banner_agent', content: '',
                                bannerText: `✓ Agent "${event.agent?.name ?? event.agent?.id}" saved`,
                            }]);
                            break;

                        case 'human_input_required': {
                            setStreamingStatus(null);
                            const firstField = Array.isArray(event.fields) && event.fields.length > 0
                                ? event.fields[0]?.name ?? 'answer'
                                : 'answer';
                            setPendingRun({ run_id: event.run_id, field: firstField });
                            setMessages((prev) => [
                                ...markLastRunningComplete(prev),
                                { role: 'assistant', kind: 'human_prompt', content: event.prompt || '' },
                            ]);
                            break;
                        }

                        case 'final':
                            setStreamingStatus(null);
                            setMessages((prev) => [
                                ...markLastRunningComplete(prev),
                                { role: 'assistant', kind: 'text', content: event.response },
                            ]);
                            break;

                        case 'error':
                            setStreamingStatus(null);
                            setMessages((prev) => [
                                ...markLastRunningComplete(prev),
                                { role: 'assistant', kind: 'text', content: `Error: ${event.message}` },
                            ]);
                            break;
                    }
                }
            }
        } catch (err: any) {
            setMessages((prev) => [
                ...markLastRunningComplete(prev),
                { role: 'assistant', kind: 'text', content: `Error: ${err.message}` },
            ]);
        } finally {
            setMessages((prev) => markLastRunningComplete(prev));
            setStreaming(false);
            setStreamingStatus(null);
            textareaRef.current?.focus();
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    };

    const agentList = agents.filter((a) => a.id !== 'synapse_builder');
    const selectedNames = agentList.filter((a) => selectedAgentIds.has(a.id)).map((a) => a.name);

    if (!isOpen) return null;

    if (collapsed) {
        return (
            <div className="fixed inset-y-0 right-0 z-50 flex items-center">
                <button
                    onClick={() => setCollapsed(false)}
                    title="Show AI Builder"
                    className="flex flex-col items-center gap-2 px-2 py-4 bg-zinc-900 border border-zinc-800 border-r-0 rounded-l-lg shadow-xl text-purple-400 hover:text-purple-300 hover:bg-zinc-800 transition-colors"
                >
                    <Sparkles size={14} />
                    <span className="text-[10px] font-semibold tracking-wide [writing-mode:vertical-rl] rotate-180 text-zinc-400">
                        AI Builder
                    </span>
                </button>
            </div>
        );
    }

    return (
        <div className="fixed inset-y-0 right-0 z-50 w-[620px] bg-zinc-950 border-l border-zinc-800 shadow-2xl flex flex-col">

            {/* ── Header ─────────────────────────────────────────────── */}
            <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-800 shrink-0">
                <div className="flex items-center gap-2">
                    <Sparkles size={15} className="text-purple-400" />
                    <span className="text-sm font-semibold text-zinc-100">AI Builder</span>
                </div>
                <div className="flex items-center gap-1">
                    <button
                        onClick={() => setCollapsed(true)}
                        title="Hide panel"
                        className="text-zinc-500 hover:text-zinc-300 transition-colors"
                    >
                        <ChevronsRight size={16} />
                    </button>
                    <button onClick={onClose} title="Close" className="text-zinc-500 hover:text-zinc-300 transition-colors">
                        <X size={16} />
                    </button>
                </div>
            </div>

            {/* ── Chat area ───────────────────────────────────────────── */}
            <div className="flex-1 overflow-y-auto px-5 py-4 space-y-3 font-sans">
                {(() => {
                    // Group consecutive activity messages; non-activity messages stay as singletons
                    type Group = { type: 'activity'; events: ChatMessage[] } | { type: 'single'; msg: ChatMessage; idx: number };
                    const groups: Group[] = [];
                    let actBuf: ChatMessage[] = [];
                    messages.forEach((msg, idx) => {
                        if (isActivity(msg)) {
                            actBuf.push(msg);
                        } else {
                            if (actBuf.length > 0) {
                                groups.push({ type: 'activity', events: [...actBuf] });
                                actBuf = [];
                            }
                            groups.push({ type: 'single', msg, idx });
                        }
                    });
                    if (actBuf.length > 0) groups.push({ type: 'activity', events: actBuf });

                    const lastGroupIdx = groups.length - 1;

                    return groups.map((group, gi) => {
                        if (group.type === 'activity') {
                            const isLive = streaming && gi === lastGroupIdx;
                            return <ActivityGroup key={`ag-${gi}`} events={group.events} isLive={isLive} />;
                        }

                        const { msg, idx } = group;

                        /* Orchestration success card */
                        if (msg.kind === 'banner_orch') {
                            return (
                                <div key={idx} className="rounded-xl border border-emerald-700/50 bg-emerald-900/20 overflow-hidden">
                                    <div className="flex items-center gap-2.5 px-4 py-3 border-b border-emerald-700/30">
                                        <CheckCircle2 size={15} className="text-emerald-400 shrink-0" />
                                        <div className="min-w-0">
                                            <p className="text-sm font-semibold text-emerald-300">Orchestration built!</p>
                                            <p className="text-[11px] text-emerald-600 truncate">
                                                &ldquo;{msg.bannerText}&rdquo; is ready in the canvas
                                            </p>
                                        </div>
                                    </div>
                                    <div className="px-4 py-2.5">
                                        <button
                                            onClick={onClose}
                                            className="flex items-center gap-1.5 text-[11px] text-emerald-400 hover:text-emerald-300 transition-colors font-medium"
                                        >
                                            View in Canvas <ArrowRight size={11} />
                                        </button>
                                    </div>
                                </div>
                            );
                        }

                        /* Agent saved — compact pill */
                        if (msg.kind === 'banner_agent') {
                            return (
                                <div key={idx} className="flex justify-center">
                                    <div className="flex items-center gap-1.5 px-3 py-1 rounded-full bg-zinc-800/60 border border-zinc-700/40 text-zinc-500 text-[10px]">
                                        <CheckCircle2 size={10} className="text-emerald-500" />
                                        {msg.bannerText}
                                    </div>
                                </div>
                            );
                        }

                        /* Human prompt from evaluator — collapsible for long content */
                        if (msg.kind === 'human_prompt') {
                            return <HumanPromptCard key={idx} content={msg.content} />;
                        }

                        /* User bubble */
                        if (msg.role === 'user') {
                            return (
                                <div key={idx} className="flex justify-end">
                                    <div className="max-w-[80%] px-4 py-2.5 rounded-2xl rounded-br-sm bg-blue-600 text-white text-sm whitespace-pre-wrap">
                                        {msg.content}
                                    </div>
                                </div>
                            );
                        }

                        /* Assistant text (final summary etc.) */
                        return (
                            <div key={idx} className="flex justify-start">
                                <div className="max-w-[88%] px-4 py-3 rounded-2xl rounded-bl-sm bg-zinc-800/80 text-zinc-200 text-sm">
                                    {renderTextContent(msg.content)}
                                </div>
                            </div>
                        );
                    });
                })()}

                {/* ── Live status indicator ── */}
                {streaming && streamingStatus && (
                    <div className="flex items-center gap-2 pl-1">
                        <div className="w-1.5 h-1.5 rounded-full bg-purple-500/70 animate-pulse shrink-0" />
                        <span className="text-[11px] text-zinc-500 font-mono">{streamingStatus}</span>
                    </div>
                )}

                <div ref={bottomRef} />
            </div>

            {/* ── Agent accordion (above input) ────────────────────────── */}
            <div className="border-t border-zinc-800 shrink-0">
                <button
                    className="w-full flex items-center justify-between px-5 py-2.5 hover:bg-zinc-900/60 transition-colors"
                    onClick={() => setAccordionOpen((v) => !v)}
                >
                    <div className="flex items-center gap-2 min-w-0">
                        <Bot size={13} className="text-zinc-500 shrink-0" />
                        {selectedNames.length === 0 && !canCreateAgents ? (
                            <span className="text-xs text-zinc-500">Select agents to include</span>
                        ) : (
                            <div className="flex items-center gap-1.5 flex-wrap">
                                {selectedNames.map((n) => (
                                    <span key={n} className="text-[11px] px-2 py-0.5 rounded-full bg-purple-900/50 text-purple-300 border border-purple-700/40">
                                        {n}
                                    </span>
                                ))}
                                {canCreateAgents && (
                                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-emerald-900/50 text-emerald-400 border border-emerald-700/40 flex items-center gap-1">
                                        <Plus size={9} /> new agents
                                    </span>
                                )}
                            </div>
                        )}
                    </div>
                    {accordionOpen
                        ? <ChevronDown size={13} className="text-zinc-500 shrink-0 ml-2" />
                        : <ChevronUp size={13} className="text-zinc-500 shrink-0 ml-2" />
                    }
                </button>

                {accordionOpen && (
                    <div className="px-4 pb-3 pt-1 max-h-56 overflow-y-auto border-t border-zinc-800/60">
                        <button
                            onClick={() => setCanCreateAgents((v) => !v)}
                            className={`mb-2 w-full flex items-center gap-2.5 px-3 py-2 rounded-lg border text-left transition-all ${
                                canCreateAgents
                                    ? 'bg-emerald-900/30 border-emerald-600/60 ring-1 ring-emerald-600/30'
                                    : 'bg-zinc-900 border-zinc-700/60 hover:border-zinc-600 hover:bg-zinc-800/60'
                            }`}
                        >
                            <div className={`w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0 transition-colors ${
                                canCreateAgents ? 'bg-emerald-600 border-emerald-600' : 'border-zinc-600'
                            }`}>
                                {canCreateAgents && (
                                    <svg width="8" height="6" viewBox="0 0 8 6" fill="none">
                                        <path d="M1 3L3 5L7 1" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                                    </svg>
                                )}
                            </div>
                            <span className="text-xs text-zinc-400">Create new agents if needed</span>
                        </button>

                        {agentList.length === 0 ? (
                            <p className="text-xs text-zinc-600 px-1">No agents configured yet.</p>
                        ) : (
                            <div className="grid grid-cols-2 gap-2">
                                {agentList.map((agent) => {
                                    const selected = selectedAgentIds.has(agent.id);
                                    return (
                                        <button
                                            key={agent.id}
                                            onClick={() => toggleAgent(agent.id)}
                                            className={`flex items-start gap-2.5 px-3 py-2.5 rounded-lg border text-left transition-all ${
                                                selected
                                                    ? 'bg-purple-900/30 border-purple-600/60 ring-1 ring-purple-600/30'
                                                    : 'bg-zinc-900 border-zinc-700/60 hover:border-zinc-600 hover:bg-zinc-800/60'
                                            }`}
                                        >
                                            <div className={`mt-0.5 w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0 transition-colors ${
                                                selected ? 'bg-purple-600 border-purple-600' : 'border-zinc-600'
                                            }`}>
                                                {selected && (
                                                    <svg width="8" height="6" viewBox="0 0 8 6" fill="none">
                                                        <path d="M1 3L3 5L7 1" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                                                    </svg>
                                                )}
                                            </div>
                                            <div className="min-w-0">
                                                <p className="text-xs font-medium text-zinc-200 truncate">{agent.name}</p>
                                                {agent.description ? (
                                                    <p className="text-[10px] text-zinc-500 mt-0.5 line-clamp-2 leading-relaxed">{agent.description}</p>
                                                ) : (
                                                    <span className={`text-[10px] px-1.5 py-0.5 rounded border mt-0.5 inline-block ${typeColor(agent.type || 'conversational')}`}>
                                                        {agent.type || 'conversational'}
                                                    </span>
                                                )}
                                            </div>
                                        </button>
                                    );
                                })}
                            </div>
                        )}
                    </div>
                )}
            </div>

            {/* ── Input + model (bottom) ───────────────────────────────── */}
            <div className="px-4 pb-4 pt-3 border-t border-zinc-800 shrink-0 space-y-2.5">
                <div className="flex items-end gap-2">
                    <textarea
                        ref={textareaRef}
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onKeyDown={handleKeyDown}
                        placeholder={pendingRun ? 'Answer the question above…' : 'Describe what you want to build…'}
                        rows={2}
                        disabled={streaming}
                        className="flex-1 bg-zinc-900 border border-zinc-700 rounded-xl px-3.5 py-2.5 text-sm text-zinc-200 placeholder-zinc-600 outline-none focus:border-purple-500/60 resize-none disabled:opacity-50 transition-colors font-sans"
                    />
                    <button
                        onClick={sendMessage}
                        disabled={!input.trim() || streaming}
                        className="flex items-center justify-center w-9 h-9 rounded-xl bg-purple-600 hover:bg-purple-500 disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors shrink-0"
                    >
                        {streaming ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
                    </button>
                </div>

                <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-zinc-600 shrink-0">Model</span>
                    <select
                        value={selectedModel}
                        onChange={(e) => setSelectedModel(e.target.value)}
                        className="bg-transparent text-[10px] text-zinc-500 hover:text-zinc-300 outline-none cursor-pointer transition-colors max-w-[160px] truncate"
                    >
                        <option value="" className="bg-zinc-900 text-zinc-300">Default</option>
                        {availableModels.map((m) => (
                            <option key={m} value={m} className="bg-zinc-900 text-zinc-300">{m}</option>
                        ))}
                    </select>
                    <span className="text-[10px] text-zinc-700 ml-auto">Enter to send · Shift+Enter for newline</span>
                </div>
            </div>
        </div>
    );
}
