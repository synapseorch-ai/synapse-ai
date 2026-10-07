'use client';
/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * The workflow editor's run log: its entries, the full-response view, a
 * Retrieve step's trace and the nested runs. Moved out of `OrchestrationTab`
 * as it was (M3a), so the open canvas's run drawer can hold it; M3a-2 moves it
 * onto the shared run screens' `applyRunEvent` and `LogLine`.
 */
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import {
    AlertTriangle, BookOpenText, Bot, Brain, Check, Circle, CircleCheck, CircleDot, CircleX, Copy, CornerDownRight,
    ExternalLink, GitBranch, GitFork, GitMerge, Info, ListTodo, MessageSquare, Minimize2, Pause, Play, RefreshCw,
    Square, Wrench, X,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Markdown, Spinner } from '@/components/ui';
import { DEGRADED, getQueryTrace, type SearchTrace } from '@/lib/knowledge';
import { RetrievalTrace } from '@/components/RetrievalTrace';

export type ToolCallLogEntry = { kind: 'tool_call'; tool_name: string; args: Record<string, any>; step_name?: string };
export type ToolResultLogEntry = { kind: 'tool_result'; tool_name: string; preview: string };
export type StepResultLogEntry = { kind: 'step_result'; step_name: string; step_type: 'agent' | 'llm' | 'print' | 'extract_json'; content: string };
// Model reasoning ([REASONING] blocks) and inter-tool prose, collapsible so a
// chatty agent doesn't drown the structural log lines.
export type ReasoningLogEntry = { kind: 'reasoning'; step_name?: string; content: string };
export type ThoughtLogEntry = { kind: 'thought'; step_name?: string; content: string };
type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'cancelled';
export type TasksLogEntry = { kind: 'tasks'; step_name?: string; tasks: { content: string; status: TaskStatus }[] };
// A structural log line: the tone picks an icon and a colour in the renderer,
// which is what replaced the emoji prefixes — an icon column reads as product
// chrome where a row of pictographs read as a group chat.
export type LineTone = 'start' | 'ok' | 'error' | 'pause' | 'loop' | 'flow' | 'parallel' | 'merge' | 'end' | 'info' | 'warn' | 'compact' | 'branch' | 'muted';
export type LineLogEntry = { kind: 'line'; tone: LineTone; text: string };
// A Retrieve step's search (R1): its trace is stored by id and fetched when the row is opened.
export type RetrievalLogEntry = { kind: 'retrieval'; step_name?: string; query_id: string; hit_count: number; cost_usd: number; degraded: string[] };
export type LogEntry = string | ToolCallLogEntry | ToolResultLogEntry | StepResultLogEntry | ReasoningLogEntry | ThoughtLogEntry | TasksLogEntry | LineLogEntry | RetrievalLogEntry;

export const line = (tone: LineTone, text: string): LineLogEntry => ({ kind: 'line', tone, text });

const LINE_META: Record<LineTone, { icon: React.FC<{ size?: number; className?: string }>; cls: string }> = {
    start:    { icon: Play,            cls: 'text-blue-400' },
    ok:       { icon: Check,           cls: 'text-green-400' },
    error:    { icon: X,               cls: 'text-red-400' },
    pause:    { icon: Pause,           cls: 'text-amber-400' },
    loop:     { icon: RefreshCw,       cls: 'text-accent' },
    flow:     { icon: GitFork,         cls: 'text-zinc-300' },
    parallel: { icon: GitBranch,       cls: 'text-zinc-300' },
    merge:    { icon: GitMerge,        cls: 'text-zinc-300' },
    end:      { icon: Square,          cls: 'text-zinc-400' },
    info:     { icon: Info,            cls: 'text-zinc-400' },
    warn:     { icon: AlertTriangle,   cls: 'text-amber-400' },
    compact:  { icon: Minimize2,       cls: 'text-zinc-400' },
    branch:   { icon: CornerDownRight, cls: 'text-text-faint pl-2' },
    muted:    { icon: Info,            cls: 'text-text-faint' },
};

interface NestedRun {
    run_id: string;
    orchestration_id: string;
    status: string;
    steps_completed: number;
    total_cost_usd?: number;
    error?: string | null;
}

/**
 * A Retrieve step's search in the run log (R1): the passage count, cost and
 * what degraded, and its trace — the legs, cuts, index versions and passages —
 * fetched the first time the row is opened (`GET /api/knowledge/queries/{id}`,
 * kept 30 days).
 */
function RetrievalRow({ entry }: { entry: RetrievalLogEntry }) {
    const [trace, setTrace] = useState<SearchTrace | null>(null);
    const [failed, setFailed] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const load = () => {
        if (trace || loading) return;
        setLoading(true);
        setFailed(null);
        getQueryTrace(entry.query_id)
            .then(setTrace)
            .catch((err) => setFailed(err instanceof Error ? err.message : String(err)))
            .finally(() => setLoading(false));
    };
    const n = entry.hit_count;
    return (
        <div className="pl-2 text-accent">
            <details onToggle={(e) => { if ((e.currentTarget as HTMLDetailsElement).open) load(); }}>
                <summary className="cursor-pointer list-none">
                    <BookOpenText size={11} className="mr-1 inline-block align-[-1px]" aria-hidden />
                    Retrieve{entry.step_name ? ` · ${entry.step_name}` : ''}: {n} passage{n !== 1 ? 's' : ''}
                    <span className="text-text-faint"> · ${entry.cost_usd.toFixed(4)}</span>
                    {/* In words, as the opened trace says them (F372); each is a sentence. */}
                    {entry.degraded.length > 0 && <span className="text-text-faint"> · {entry.degraded.map(code => DEGRADED[code] ?? code).join(' ')}</span>}
                </summary>
                <div className="mt-0.5 rounded-md bg-zinc-800/50 px-2 py-1 text-zinc-300">
                    {loading && <Spinner />}
                    {failed && <span className="text-red-300">Trace unavailable: {failed}</span>}
                    {trace && <RetrievalTrace trace={trace} withDegraded withHits dense />}
                    <div className="pt-0.5 text-[10px] text-text-faint" translate="no">{entry.query_id}</div>
                </div>
            </details>
        </div>
    );
}

/** The runs nested directly under this run — one per run of a nested step (F251). */
export function NestedRuns({ runId, refreshKey, names }: { runId: string; refreshKey: string; names: Record<string, string> }) {
    const [children, setChildren] = useState<NestedRun[]>([]);
    useEffect(() => {
        let live = true;
        fetch(`/api/orchestrations/runs/${encodeURIComponent(runId)}/children`)
            .then(r => (r.ok ? r.json() : []))
            .then(rows => { if (live) setChildren(Array.isArray(rows) ? rows : []); })
            .catch(() => { if (live) setChildren([]); });
        return () => { live = false; };
    }, [runId, refreshKey]);
    if (children.length === 0) return null;
    return (
        <div className="rounded-md border border-border bg-surface-2/40 px-2.5 py-2">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-text-faint mb-1">
                Nested runs ({children.length})
            </div>
            <ul className="space-y-0.5">
                {children.map(c => (
                    <li key={c.run_id} className="flex items-baseline gap-2 text-[11px]">
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                            c.status === 'completed' ? 'bg-green-400' :
                            c.status === 'failed' ? 'bg-red-400' :
                            c.status === 'cancelled' ? 'bg-zinc-500' :
                            c.status === 'paused' ? 'bg-yellow-400' : 'bg-blue-400 animate-pulse'
                        }`} aria-hidden />
                        <span className="text-zinc-300 truncate">{names[c.orchestration_id] || c.orchestration_id}</span>
                        <span className="text-text-faint capitalize">{c.status}</span>
                        <span className="text-text-faint">
                            {c.steps_completed} step{c.steps_completed === 1 ? '' : 's'}
                        </span>
                        {c.error && <span className="text-red-300/90 truncate" title={c.error}>{c.error}</span>}
                    </li>
                ))}
            </ul>
        </div>
    );
}

// --- Response detail modal ---
// The full document treatment — styled headings, lists, tables, code — shared
// by every modal that shows model-written markdown. The human-input context
// used to render through a four-entry map at text-xs, which flattened a
// report's headings into cramped body text while the agent-response modal
// next to it looked finished.

export function ResponseModal({ stepName, stepType, content, onClose }: { stepName: string; stepType?: string; content: string; onClose: () => void }) {
    // Try to pretty-print JSON for extract_json steps
    const isJson = stepType === 'extract_json';
    let formattedJson = content;
    let jsonParseOk = false;
    if (isJson) {
        try {
            const parsed = JSON.parse(content);
            formattedJson = JSON.stringify(parsed, null, 2);
            jsonParseOk = true;
        } catch {
            // content may already be pretty or non-JSON — just display as-is
            formattedJson = content;
        }
    }
    const [copied, setCopied] = useState(false);
    useEffect(() => {
        const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        document.addEventListener('keydown', handler);
        return () => document.removeEventListener('keydown', handler);
    }, [onClose]);

    return createPortal(
        <div
            className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
            onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
            {/* Backdrop */}
            <div className="absolute inset-0 bg-scrim/70 backdrop-blur-sm" />
            {/* Panel */}
            <div className="relative z-10 w-full max-w-3xl max-h-[80vh] flex flex-col rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl">
                {/* Header */}
                <div className="flex items-center justify-between px-5 py-3 border-b border-zinc-700 shrink-0">
                    <div className="flex items-center gap-2">
                        <Bot size={14} className="text-blue-400" />
                        <span className="text-sm font-semibold text-zinc-100">{stepName}</span>
                        <span className="text-xs text-text-faint">— full response</span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            onClick={() => { navigator.clipboard.writeText(isJson ? formattedJson : content); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
                            className="text-zinc-400 hover:text-zinc-100 transition-colors p-1 rounded-md hover:bg-zinc-700"
                            title="Copy to clipboard"
                        >
                            {copied ? <Check size={15} className="text-green-400" /> : <Copy size={15} />}
                        </button>
                        <button onClick={onClose} className="text-zinc-400 hover:text-zinc-100 transition-colors p-1 rounded-md hover:bg-zinc-700">
                            <X size={15} />
                        </button>
                    </div>
                </div>
                {/* Content */}
                <div className="flex-1 overflow-y-auto p-5">
                    {isJson ? (
                        /* Pretty-printed JSON with basic syntax colouring */
                        <pre className="text-xs font-code leading-relaxed whitespace-pre-wrap break-all rounded-lg bg-zinc-800/70 border border-zinc-700/50 p-4 overflow-x-auto">
                            {(jsonParseOk ? formattedJson : content)
                                .split('\n')
                                .map((line, i) => {
                                    /* Colour keys orange, strings lime, numbers/booleans/null cyan */
                                    const coloured = line
                                        .replace(/("(?:[^"\\]|\\.)*")(\s*:)/g, '<span class="text-orange-300">$1</span><span class="text-zinc-400">$2</span>')
                                        .replace(/:\s*("(?:[^"\\]|\\.)*")/g, ': <span class="text-lime-300">$1</span>')
                                        .replace(/:\s*(\d+\.?\d*)/g, ': <span class="text-cyan-300">$1</span>')
                                        .replace(/:\s*(true|false|null)\b/g, ': <span class="text-cyan-400">$1</span>');
                                    return (
                                        <span key={i} dangerouslySetInnerHTML={{ __html: coloured + '\n' }} />
                                    );
                                })}
                        </pre>
                    ) : (
                    <Markdown className="max-w-none">{content}</Markdown>
                    )}
                </div>
            </div>
        </div>,
        document.body
    );
}

/** Every entry of the run log, as the editor's Run Log tab drew them. */
export function RunLogEntries({ runLog, onOpenResponseModal }: {
    runLog: LogEntry[];
    onOpenResponseModal: (entry: { step_name: string; step_type?: string; content: string }) => void;
}) {
    return (
        <div className="font-mono text-[11px] text-zinc-400 space-y-0.5">
            {runLog.length === 0 ? (
                <div className="text-text-faint italic">No run output yet. Click Run now to start.</div>
            ) : (
                runLog.map((entry, i) => {
                    if (typeof entry !== 'string') {
                        if (entry.kind === 'line') {
                            const meta = LINE_META[entry.tone];
                            const LineIcon = meta.icon;
                            return (
                                <div key={i} className={`flex items-start gap-1.5 ${meta.cls}`}>
                                    <LineIcon size={11} className="mt-[3px] shrink-0" aria-hidden />
                                    {/* Plain text, not markdown. These lines are built
                                        above out of event fields — an expression, a step
                                        name, a tool name, a customer's own data — and none
                                        of them is markdown anyone wrote. Rendering them as
                                        markdown ate the underscores in
                                        `().__class__.__bases__[0].__subclasses__()`, so a
                                        run log explaining which attribute the engine
                                        refused named a different attribute. */}
                                    <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{entry.text}</span>
                                </div>
                            );
                        }
                        if (entry.kind === 'tool_call') {
                            return (
                                <div key={i} className="text-accent pl-2">
                                    <details>
                                        <summary className="cursor-pointer list-none">
                                            <Wrench size={11} className="mr-1 inline-block align-[-1px]" aria-hidden />
                                            {entry.tool_name}
                                            {entry.step_name && <span className="text-text-faint text-[10px]"> · {entry.step_name}</span>}
                                        </summary>
                                        <pre className="font-code bg-zinc-800/50 p-1 rounded-md mt-0.5 text-[10px] text-zinc-300 overflow-x-auto whitespace-pre-wrap">
                                            {JSON.stringify(entry.args, null, 2)}
                                        </pre>
                                    </details>
                                </div>
                            );
                        }
                        if (entry.kind === 'retrieval') {
                            return <RetrievalRow key={i} entry={entry} />;
                        }
                        if (entry.kind === 'tool_result') {
                            return (
                                <div key={i} className="flex items-start gap-1.5 pl-4 text-[10px] text-text-faint">
                                    <CornerDownRight size={10} className="mt-[2px] shrink-0" aria-hidden />
                                    <span className="min-w-0 flex-1">{entry.preview.slice(0, 200)}{entry.preview.length > 200 ? '…' : ''}</span>
                                </div>
                            );
                        }
                        if (entry.kind === 'step_result') {
                            const preview = entry.content.slice(0, 200).replace(/\n+/g, ' ').trim();
                            const isTruncated = entry.content.length > 200;
                            const isAgent = entry.step_type === 'agent';
                            const isPrint = entry.step_type === 'print';
                            const isExtractJson = entry.step_type === 'extract_json';
                            const dotColor = isAgent ? 'bg-emerald-400' : isPrint ? 'bg-lime-400' : isExtractJson ? 'bg-orange-400' : 'bg-teal-400';
                            const labelColor = isAgent ? 'text-emerald-400' : isPrint ? 'text-lime-400' : isExtractJson ? 'text-orange-400' : 'text-teal-400';
                            const typeLabel = isAgent ? 'agent response' : isPrint ? 'print output' : isExtractJson ? 'json output' : 'llm output';
                            return (
                                <div key={i} className="my-1.5">
                                    {/* Header label */}
                                    <div className="flex items-center gap-1.5 mb-1 pl-1">
                                        <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${dotColor}`} />
                                        <span className={`text-[10px] font-semibold uppercase tracking-wider ${labelColor}`}>
                                            {entry.step_name}
                                        </span>
                                        <span className="text-[9px] text-text-faint uppercase tracking-wide">
                                            {typeLabel}
                                        </span>
                                    </div>
                                    {/* Content bubble \u2014 the whole card opens the full
                                        response; the hover hint is just the signpost. */}
                                    <button
                                        type="button"
                                        onClick={() => onOpenResponseModal({ step_name: entry.step_name, step_type: entry.step_type, content: entry.content })}
                                        title="View full response"
                                        className="group ml-3 flex w-[calc(100%-0.75rem)] items-start gap-2 rounded-lg border border-zinc-700/50 bg-zinc-800/70 px-3 py-2 text-left transition-colors hover:border-zinc-600 hover:bg-zinc-800"
                                    >
                                        <span className="min-w-0 flex-1 leading-relaxed text-zinc-300">
                                            {preview}{isTruncated ? '\u2026' : ''}
                                        </span>
                                        <span className="ml-1 flex shrink-0 items-center gap-1 self-center whitespace-nowrap text-[10px] text-text-faint opacity-0 transition-opacity group-hover:opacity-100">
                                            <ExternalLink size={10} aria-hidden />
                                            View full
                                        </span>
                                    </button>
                                </div>
                            );
                        }
                        if (entry.kind === 'tasks') {
                            const done = entry.tasks.filter(t => t.status === 'completed').length;
                            return (
                                <div key={i} className="my-1.5 ml-1 rounded-lg border border-zinc-700/50 bg-zinc-800/40 px-3 py-2">
                                    <div className="flex items-center gap-1.5 mb-1">
                                        <ListTodo size={11} className="text-accent shrink-0" aria-hidden />
                                        <span className="text-[10px] font-semibold uppercase tracking-wider text-zinc-400">Tasks</span>
                                        <span className="text-[9px] text-text-faint">
                                            {done}/{entry.tasks.length} done{entry.step_name ? ` · ${entry.step_name}` : ''}
                                        </span>
                                    </div>
                                    <ul className="space-y-0.5">
                                        {entry.tasks.map((t, j) => (
                                            <li key={j} className="flex items-start gap-1.5 text-[11px] leading-snug">
                                                {t.status === 'completed' ? (
                                                    <CircleCheck size={11} className="mt-[2px] shrink-0 text-emerald-400" aria-hidden />
                                                ) : t.status === 'in_progress' ? (
                                                    <CircleDot size={11} className="mt-[2px] shrink-0 text-accent" aria-hidden />
                                                ) : t.status === 'cancelled' ? (
                                                    <CircleX size={11} className="mt-[2px] shrink-0 text-text-faint" aria-hidden />
                                                ) : (
                                                    <Circle size={11} className="mt-[2px] shrink-0 text-text-faint" aria-hidden />
                                                )}
                                                <span className={
                                                    t.status === 'completed' ? 'text-text-faint line-through' :
                                                    t.status === 'in_progress' ? 'text-zinc-200' :
                                                    t.status === 'cancelled' ? 'text-text-faint line-through' :
                                                    'text-zinc-400'
                                                }>{t.content}</span>
                                            </li>
                                        ))}
                                    </ul>
                                </div>
                            );
                        }
                        if (entry.kind === 'reasoning' || entry.kind === 'thought') {
                            const isReasoning = entry.kind === 'reasoning';
                            const firstLine = entry.content.split('\n')[0].slice(0, 120);
                            return (
                                <div key={i} className="pl-2 my-0.5">
                                    <details>
                                        <summary className={`cursor-pointer list-none text-[10px] ${isReasoning ? 'text-sky-400/80' : 'text-text-faint'}`}>
                                            {isReasoning
                                                ? <Brain size={10} className="mr-1 inline-block align-[-1px]" aria-hidden />
                                                : <MessageSquare size={10} className="mr-1 inline-block align-[-1px]" aria-hidden />}
                                            {firstLine}{entry.content.length > firstLine.length ? '…' : ''}
                                            {entry.step_name && <span className="text-text-faint"> · {entry.step_name}</span>}
                                        </summary>
                                        <div className="mt-0.5 bg-zinc-800/40 border border-zinc-800 rounded-md p-2 text-[10px] text-zinc-400 whitespace-pre-wrap max-h-64 overflow-y-auto">
                                            {entry.content}
                                        </div>
                                    </details>
                                </div>
                            );
                        }
                        return null;
                    }
                    return (
                        <div key={i} className={
                            entry.startsWith('✓') ? 'text-green-400' :
                            entry.startsWith('✗') ? 'text-red-400' :
                            entry.startsWith('▶') ? 'text-blue-400' :
                            entry.startsWith('⏸') ? 'text-amber-400' :
                            entry.startsWith('⟳') ? 'text-accent' :
                            'text-zinc-400'
                        }>
                            <ReactMarkdown
                                remarkPlugins={[remarkGfm]}
                                components={{
                                    p: ({ children }) => <span>{children}</span>,
                                    code: ({ children }) => <code className="font-code bg-surface-2 px-1 rounded-md text-[10px]">{children}</code>,
                                    pre: ({ children }) => <pre className="font-code bg-surface-2 p-1 rounded-md mt-0.5 overflow-x-auto">{children}</pre>,
                                    a: ({ href, children }) => <a href={href} className="underline opacity-70" target="_blank" rel="noreferrer">{children}</a>,
                                }}
                            >{entry}</ReactMarkdown>
                        </div>
                    );
                })
            )}
        </div>
    );
}
