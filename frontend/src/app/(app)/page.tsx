'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import { Send, Bot, User, Terminal, Plus, Server, ChevronDown, ChevronRight, Zap, GitBranch, CheckCircle2, AlertCircle, History, RefreshCw, Clock, Trash2, X, Paperclip, Cpu, Wrench, Network, CalendarClock, Sparkles, Copy, Check, Brain, Square, Pencil, RotateCcw} from 'lucide-react';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { CollectDataForm } from '@/components/CollectDataForm';
import { HumanStepAnswer } from '@/components/HumanStepAnswer';
import { ActiveRunsBanner } from '@/components/ActiveRunsBanner';
import { AttachButton, AttachmentChips, AttachmentError, useImageAttachments } from '@/components/app/ImageAttachments';
import { Markdown, Spinner } from '@/components/ui';

import { cn } from '@/lib/utils';
import { readWithStallTimeout } from '@/lib/sse';
import { refusedAnswerMessage } from '@/lib/answers';
import { Message, OrchMsgType, SystemStatus } from '@/types';

// ─── LLM Thought Collapsible ────────────────────────────────────────────────
type ThoughtEntry =
  | { type: 'prose'; text: string }
  | { type: 'tool_call'; toolName: string };

/**
 * Whose work this is, when it is not the agent the person asked.
 *
 * An agent that delegates keeps streaming: the sub-agent's thinking, tool calls
 * and results all arrive on the same channel, and the activity line read them
 * as the main agent's own — the log said the agent you asked searched the web
 * when a different agent did. The engine has always tagged them; the route now
 * carries the tag through (F7).
 */
function delegatePrefix(data: Record<string, unknown>): string {
  const name = data.delegate_agent_name;
  return typeof name === 'string' && name.trim() ? `${name.trim()} › ` : '';
}

/**
 * A turn's thoughts to keep beside its answer: the last one goes when it only
 * repeats the answer (F102). The engine sends each turn's text as
 * `llm_thought`, the answering turn included, so the panel of the agent's turns
 * ended with the answer itself. Cloud's chat drops it the same way
 * (`org/chat/turn.ts`).
 */
function withoutEcho(thoughts: string[], answer: unknown): string[] {
  const said = typeof answer === 'string' ? answer.trim() : '';
  if (!said || thoughts.length === 0) return thoughts;
  return thoughts[thoughts.length - 1].trim() === said ? thoughts.slice(0, -1) : thoughts;
}

function ThoughtCollapsible({ thoughts, stepName }: { thoughts: string[]; stepName?: string }) {
  const [open, setOpen] = useState(false);

  // Map every thought — prose stays as-is, pure JSON tool calls become a readable summary
  const parsedThoughts: ThoughtEntry[] = thoughts
    .map(t => {
      const trimmed = t.trim();
      if (!trimmed) return null;
      if (trimmed.startsWith('{')) {
        try {
          const obj = JSON.parse(trimmed);
          const toolName = (obj.tool || obj.name || 'tool') as string;
          return { type: 'tool_call' as const, toolName };
        } catch { /* not pure JSON — fall through */ }
      }
      return { type: 'prose' as const, text: trimmed };
    })
    .filter(Boolean) as ThoughtEntry[];

  if (parsedThoughts.length === 0) return null;

  return (
    <div className="mt-2 border border-zinc-700 rounded-sm overflow-hidden w-0 min-w-full">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] font-mono text-zinc-300 hover:text-zinc-100 hover:bg-zinc-900/60 bg-zinc-900/30 transition-colors"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <span className="uppercase tracking-wider">
          💭 Reasoning {stepName ? `· ${stepName}` : ''} ({parsedThoughts.length} turn{parsedThoughts.length > 1 ? 's' : ''})
        </span>
      </button>
      {open && (
        <div className="border-t border-zinc-800 bg-zinc-950/60 divide-y divide-zinc-900">
          {parsedThoughts.map((entry, i) => (
            <div key={i} className="px-3 py-2 text-[12px] font-mono leading-5">
              {parsedThoughts.length > 1 && (
                <span className="text-text-faint text-[10px] mr-2">Turn {i + 1}</span>
              )}
              {entry.type === 'tool_call' ? (
                <span className="text-text-faint">→ Called: {entry.toolName}</span>
              ) : (
                <span className="text-zinc-400 whitespace-pre-wrap break-words">{entry.text}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── LLM Reasoning Collapsible ──────────────────────────────────────────────
// Renders private chain-of-thought extracted server-side from [REASONING]
// blocks. One entry per turn, in chronological order.
function ReasoningCollapsible({ reasoning, stepName }: { reasoning: string[]; stepName?: string }) {
  const [open, setOpen] = useState(false);
  const blocks = reasoning.map(r => r.trim()).filter(Boolean);
  if (blocks.length === 0) return null;
  return (
    <div className="mt-2 border border-indigo-900/40 rounded-sm overflow-hidden w-0 min-w-full">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-[11px] font-mono text-indigo-300 hover:text-indigo-100 hover:bg-indigo-950/40 bg-indigo-950/20 transition-colors"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <Brain className="h-3 w-3" />
        <span className="uppercase tracking-wider">
          Thinking {stepName ? `· ${stepName}` : ''} ({blocks.length} turn{blocks.length > 1 ? 's' : ''})
        </span>
      </button>
      {open && (
        <div className="border-t border-indigo-900/30 bg-indigo-950/10 divide-y divide-indigo-900/20">
          {blocks.map((block, i) => (
            <div key={i} className="px-3 py-2 text-[12px] font-mono italic leading-5">
              {blocks.length > 1 && (
                <span className="text-indigo-700 text-[10px] not-italic mr-2">Turn {i + 1}</span>
              )}
              <span className="text-indigo-300/80 whitespace-pre-wrap break-words">{block}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Orchestration Step Divider ──────────────────────────────────────────────
function StepDivider({ stepName, stepType }: { stepName: string; stepType?: string }) {
  const typeIcon: Record<string, string> = {
    agent: '🤖', evaluator: '🔀', parallel: '⚡', merge: '🔗',
    loop: '🔄', human: '👤', transform: '⚙️', end: '🏁',
  };
  const icon = typeIcon[stepType || ''] ?? '▶';
  return (
    <div className="flex items-center gap-3 my-1 px-1">
      <div className="h-px flex-1 bg-gradient-to-r from-transparent to-zinc-800" />
      <div className="flex items-center gap-1.5 text-[11px] font-mono uppercase tracking-widest text-text-faint shrink-0">
        <span>{icon}</span>
        <span>{stepName}</span>
        {stepType && <span className="text-text-faint">· {stepType}</span>}
      </div>
      <div className="h-px flex-1 bg-gradient-to-l from-transparent to-zinc-800" />
    </div>
  );
}

// ─── Orchestration Info Banner ───────────────────────────────────────────────
function OrchBanner({ content, variant }: { content: string; variant: 'start' | 'complete' | 'error' }) {
  const styles: Record<string, string> = {
    start: 'border-accent/40 bg-accent-subtle text-accent',
    complete: 'border-emerald-900/50 bg-emerald-950/20 text-emerald-400',
    error: 'border-red-900/50 bg-red-950/20 text-red-400',
  };
  return (
    <div className="flex justify-center my-2">
      <div className={cn('border px-5 py-1.5 text-[11px] font-mono uppercase tracking-widest rounded-sm', styles[variant])}>
        {content}
      </div>
    </div>
  );
}

// ─── Sub-agent Result Bubble ─────────────────────────────────────────────────
function AgentStepResult({ msg, onCollectDataSubmit }: {
  msg: Message;
  onCollectDataSubmit: (values: Record<string, unknown>) => void;
}) {
  // Fallback chain: content → data.content → data.result → empty
  const displayContent = msg.content || (msg.data as any)?.content || (msg.data as any)?.result || '';
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex gap-3 max-w-4xl">
      <div className="h-7 w-7 shrink-0 flex items-center justify-center border border-accent/40 bg-accent-subtle text-accent mt-1 rounded-sm">
        <Bot className="h-3.5 w-3.5" />
      </div>
      <div className="flex flex-col flex-1 min-w-0 gap-2">
        {msg.stepName && (
          <div className="text-[10px] uppercase tracking-widest text-accent/70 font-mono">
            {msg.stepName}
          </div>
        )}
        {msg.reasoning && msg.reasoning.length > 0 && (
          <ReasoningCollapsible reasoning={msg.reasoning} stepName={msg.stepName} />
        )}
        <div className="p-3 text-[14px] leading-7 border border-accent/40 bg-accent-subtle relative font-sans rounded-sm group">
          <Markdown>{displayContent}</Markdown>
          <button
            onClick={() => { navigator.clipboard.writeText(displayContent); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
            className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded-md bg-accent-subtle hover:bg-accent/20 text-accent hover:text-accent-hover"
            title="Copy response"
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          </button>
        </div>
        {/* Thoughts */}
        {msg.thoughts && msg.thoughts.length > 0 && (
          <ThoughtCollapsible thoughts={msg.thoughts} stepName={msg.stepName} />
        )}
        {/* Intent-based UI */}
        <div className="w-full pl-1">
          {msg.intent === 'collect_data' && msg.data && (
            <CollectDataForm data={msg.data} onSubmit={onCollectDataSubmit} />
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Assistant Message Bubble ────────────────────────────────────────────────
function AssistantBubble({ msg, agentName }: { msg: Message; agentName: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex flex-col self-start min-w-72 max-w-full gap-2">
      {msg.reasoning && msg.reasoning.length > 0 && (
        <ReasoningCollapsible reasoning={msg.reasoning} />
      )}
      <div className={cn(
        "text-[15px] leading-7 border relative font-sans px-4 pb-4 bg-zinc-900/50 border-zinc-800 text-zinc-100 w-full group",
        msg.intent ? "pt-6" : "pt-4"
      )}>
        {msg.intent && (
          <div className="absolute -top-3 left-2 bg-zinc-950 border border-zinc-800 px-2 py-0.5 text-[10px] uppercase tracking-wider text-zinc-400 font-mono rounded-md">
            {agentName}
          </div>
        )}
        <Markdown>{msg.content}</Markdown>
        {msg.images && msg.images.length > 0 && (
          <div className="flex flex-wrap gap-3 mt-4">
            {msg.images.map((img, imgIdx) => (
              <div key={imgIdx} className="relative group">
                <img
                  src={img}
                  alt={`Attached ${imgIdx + 1}`}
                  className="h-[4.5rem] w-[4.5rem] object-cover border border-zinc-700/60 rounded-xl cursor-pointer hover:border-zinc-400 hover:scale-[1.03] hover:shadow-lg transition-all"
                  onClick={() => window.open(img, '_blank')}
                />
              </div>
            ))}
          </div>
        )}
        <button
          onClick={() => { navigator.clipboard.writeText(msg.content); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
          className="absolute bottom-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity p-1 rounded-md bg-zinc-800/60 hover:bg-zinc-700/80 text-text-faint hover:text-zinc-200"
          title="Copy response"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
        </button>
      </div>
      {msg.thoughts && msg.thoughts.length > 0 && (
        <ThoughtCollapsible thoughts={msg.thoughts} />
      )}
    </div>
  );
}

// ─── Types ───────────────────────────────────────────────────────────────────
type SessionSummary = {
  session_id: string;
  agent_id: string;
  last_response: string | null;
  last_updated: string | null;
  turn_count: number;
  first_user_message: string | null;
};

type SessionTurn = {
  user: string;
  assistant: string;
  tools: string[];
  timestamp: string;
};

/** A tool call the conversation stopped to ask about — the card, without the loop's state. */
type ParkedApproval = {
  kind: 'tool_approval';
  tool_name: string;
  tool_args: Record<string, unknown>;
  category: string;
  approval_id: string;
  message: string;
  prompt: string;
};

/** A turn still being written. A turn is saved when it ends, so it is not in `turns` yet. */
type RunningTurn = { turn_id: string; message: string; started_at: number; cursor: string };

type StepState = {
  thoughts: string[];
  reasoning: string[];
  orchStepId: string | null;
  orchCompleted: boolean;
  receivedAgentStep: boolean;
};

const newStepState = (): StepState => (
  { thoughts: [], reasoning: [], orchStepId: null, orchCompleted: false, receivedAgentStep: false }
);

function formatRelativeTime(iso: string | null): string {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// ─── Tip data ────────────────────────────────────────────────────────────────
const TIPS = [
  {
    icon: <Cpu className="h-5 w-5 text-accent" />,
    tag: 'Build Agents',
    title: 'Create specialized AI agents',
    desc: 'Design agents with custom system prompts, tools, and models. Each agent can be fine-tuned for a specific domain or task like research, coding, or analysis.',
    accent: 'from-accent/10 to-transparent border-accent/40',
    tagColor: 'text-accent bg-accent-subtle border-accent/40',
  },
  {
    icon: <Wrench className="h-5 w-5 text-sky-400" />,
    tag: 'Custom Tools',
    title: 'Extend with custom tools',
    desc: 'Add Python functions, n8n workflows, or custom APIs as tools your agents can call — web search, database queries, file operations — anything your workflow needs.',
    accent: 'from-sky-500/10 to-transparent border-sky-800/40',
    tagColor: 'text-sky-400 bg-sky-950/60 border-sky-800/50',
  },
  {
    icon: <GitBranch className="h-5 w-5 text-emerald-400" />,
    tag: 'Orchestration',
    title: 'Orchestrate multi-agent flows',
    desc: 'Chain agents together with sequential, parallel, or loop patterns. Build complex pipelines where each agent handles a specialized stage.',
    accent: 'from-emerald-500/10 to-transparent border-emerald-800/40',
    tagColor: 'text-emerald-400 bg-emerald-950/60 border-emerald-800/50',
  },
  {
    icon: <Network className="h-5 w-5 text-amber-400" />,
    tag: 'MCP Servers',
    title: 'Connect MCP servers',
    desc: 'Integrate any MCP (Model Context Protocol) server to supercharge your agents with external knowledge, file systems, browsers, and third-party services.',
    accent: 'from-amber-500/10 to-transparent border-amber-800/40',
    tagColor: 'text-amber-400 bg-amber-950/60 border-amber-800/50',
  },
  {
    icon: <CalendarClock className="h-5 w-5 text-rose-400" />,
    tag: 'Schedules',
    title: 'Automate with schedules',
    desc: 'Set up cron-based schedules to run your agents automatically. Perfect for daily reports, periodic data syncs, or recurring agentic tasks.',
    accent: 'from-rose-500/10 to-transparent border-rose-800/40',
    tagColor: 'text-rose-400 bg-rose-950/60 border-rose-800/50',
  },
  {
    icon: <Sparkles className="h-5 w-5 text-accent" />,
    tag: 'Pro Tips',
    title: 'Get the most from Synapse AI',
    desc: 'Attach images for vision tasks, switch agents mid-conversation, or orchestrate complex multi-step workflows — all from one chat interface.',
    accent: 'from-accent/10 to-transparent border-accent/40',
    tagColor: 'text-accent bg-accent-subtle border-accent/40',
  },
];

// ─── Welcome Screen ──────────────────────────────────────────────────────────
/**
 * The agent wants to run something the policy will not let it run unasked.
 *
 * Deliberately not a `CollectDataForm`: that renders a question with fields,
 * and this is a decision with a subject. What matters is being able to read
 * the exact call — `run_sql_query` is one door for SELECT and DROP, and
 * "allow this tool?" without the arguments is not a question anyone can answer.
 */
function ToolApprovalCard({
  msg, onDecide, disabled,
}: {
  msg: Message;
  onDecide: (approve: boolean, reason: string) => void;
  disabled: boolean;
}) {
  const [reason, setReason] = useState('');
  const [answered, setAnswered] = useState(false);
  const args = msg.data?.tool_args as Record<string, unknown> | undefined;

  return (
    <div className="flex gap-4 max-w-4xl">
      <div className="h-8 w-8 shrink-0 flex items-center justify-center border border-amber-700/50 bg-amber-950/30 text-amber-400 rounded-md">
        <AlertCircle className="h-4 w-4" />
      </div>
      <div className="flex-1 min-w-0 p-4 border border-amber-900/40 bg-amber-950/10 relative font-sans rounded-md">
        <div className="absolute -top-3 left-2 bg-zinc-950 border border-amber-900/50 px-2 py-0.5 text-[10px] uppercase tracking-wider text-amber-500 font-mono rounded-md">
          Permission needed
        </div>
        <Markdown className="text-[15px] leading-7">{msg.content}</Markdown>

        {args && Object.keys(args).length > 0 && (
          <pre className="mt-3 p-3 bg-zinc-950 border border-zinc-800 rounded-md text-[12px] font-code text-zinc-300 overflow-x-auto">
            {JSON.stringify(args, null, 2)}
          </pre>
        )}

        {answered ? (
          <p className="mt-3 text-[12px] text-text-faint">Answered.</p>
        ) : (
          <>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              aria-label="Reason for refusing"
              placeholder="Why not? (optional — the agent reads this and works around it)"
              disabled={disabled}
              className="mt-3 w-full bg-zinc-950 border border-zinc-800 px-3 py-2 text-[13px] text-zinc-200 placeholder:text-zinc-600 rounded-md focus:border-amber-700/60 focus:outline-none disabled:opacity-50"
            />
            <div className="flex gap-2 mt-3">
              <button
                type="button"
                disabled={disabled}
                onClick={() => { setAnswered(true); onDecide(true, ''); }}
                className="px-3 py-1.5 bg-accent text-accent-fg text-[11px] font-semibold uppercase tracking-wider rounded-md hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                Allow once
              </button>
              <button
                type="button"
                disabled={disabled}
                onClick={() => { setAnswered(true); onDecide(false, reason); }}
                className="px-3 py-1.5 border border-zinc-700 text-zinc-300 text-[11px] font-semibold uppercase tracking-wider rounded-md hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                Refuse
              </button>
            </div>
            <p className="mt-2 text-[11px] text-text-faint">
              Allowing applies to this call and these arguments only.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

/** A sentence from the backend, with `ollama pull …` shown as code. */
function WithCode({ text }: { text: string }) {
  return (
    <>
      {text.split('`').map((part, i) => (i % 2
        ? <code key={i} className="font-code text-[12px] text-text">{part}</code>
        : <span key={i}>{part}</span>))}
    </>
  );
}

/** Where every "the model cannot answer" message points (F182). */
const MODELS_SETTINGS = '/settings/models';

function WelcomeScreen({ agentName, modelReady, modelProblem, onPrompt, onNavigate, showExamplesBanner, onDismissBanner }: { agentName: string; modelReady?: boolean; modelProblem?: string | null; onPrompt: (text: string) => void; onNavigate: (path: string) => void; showExamplesBanner: boolean; onDismissBanner: () => void; }) {
  const [activeIdx, setActiveIdx] = useState(0);
  const [isPaused, setIsPaused] = useState(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Auto-advance carousel
  useEffect(() => {
    if (isPaused) return;
    intervalRef.current = setInterval(() => {
      setActiveIdx(prev => (prev + 1) % TIPS.length);
    }, 3500);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [isPaused]);

  // These used to point at /settings/* — the chat page telling you to open
  // Settings in order to use the product.
  const PILLS = [
    { label: 'Build an agent', path: '/agents', icon: Bot },
    { label: 'Add a tool', path: '/tools', icon: Wrench },
    { label: 'Orchestrate agents', path: '/orchestrations', icon: Network },
    { label: 'Connect MCP', path: '/mcp-servers', icon: Server },
    { label: 'Schedule a task', path: '/schedules', icon: CalendarClock },
  ];

  return (
    <div className="flex flex-col items-center justify-center min-h-full px-4 pb-28 pt-6 md:pb-36 md:pt-10 select-none">
      {/* ── Welcome Examples Banner (12-day, session-dismissible) ── */}
      {showExamplesBanner && (
        <div className="w-full max-w-2xl mb-8 relative overflow-hidden rounded-sm border border-accent/40 bg-accent-subtle bg-gradient-to-br from-accent/15 to-transparent backdrop-blur-sm shadow-lg shadow-accent/20">
          {/* Subtle animated shimmer */}
          <div className="absolute inset-0 bg-gradient-to-r from-transparent via-accent/5 to-transparent -skew-x-12 pointer-events-none" />
          <div className="relative px-4 py-3 flex flex-col gap-2.5">
            {/* Top row: icon + headline + close */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-2.5 min-w-0 flex-1">
                <div className="p-1.5 rounded-sm bg-accent/20 border border-accent/30 shrink-0">
                  <Sparkles className="h-3.5 w-3.5 text-accent" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-[12px] font-bold text-text tracking-wide">Get started in seconds</p>
                  <p className="text-[11px] text-accent font-mono break-words">Pre-built agents, orchestrations &amp; MCP servers — ready to explore</p>
                </div>
              </div>
              <button
                id="examples-banner-close"
                onClick={onDismissBanner}
                className="p-1 text-accent hover:text-accent-hover transition-colors cursor-pointer shrink-0 mt-0.5"
                aria-label="Dismiss banner"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            {/* Bottom row: teaser pills + CTA */}
            <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
              <div className="flex items-center gap-1.5 flex-wrap justify-center sm:justify-start">
                {['Web research agent', 'Multi-step orchestration', 'MCP Servers'].map(label => (
                  <span key={label} className="px-2 py-0.5 text-[10px] font-mono text-accent bg-accent/10 border border-accent/40 rounded-sm">
                    {label}
                  </span>
                ))}
                <span className="text-[10px] font-mono text-accent">+ more →</span>
              </div>
              <button
                id="examples-banner-link"
                onClick={() => onNavigate('/settings/import_export?tab=examples')}
                className="flex items-center justify-center gap-1.5 px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider bg-accent hover:bg-accent-hover active:bg-accent text-accent-fg transition-colors rounded-sm cursor-pointer shrink-0 w-full sm:w-auto shadow-sm shadow-accent/20"
              >
                <Sparkles className="h-3 w-3" /> Browse Examples
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Greeting. "Ready" is said only once the backend has checked that the
          model can answer; it used to be said unconditionally, above a first
          message that then failed with an HTTP library's text (F182). */}
      <div className="mb-2 flex items-center gap-2">
        <div className={cn(
          "h-1.5 w-1.5 rounded-full",
          modelReady === true ? "bg-green-400 shadow-[0_0_8px_#4ade80] animate-pulse"
            : modelReady === false ? "bg-warning" : "bg-zinc-600",
        )} />
        <span className="text-[11px] font-mono uppercase tracking-[0.2em] text-text-faint">
          {agentName}{modelReady === true ? '\u00a0· Ready' : modelReady === false ? '\u00a0· Model not ready' : ''}
        </span>
      </div>
      {modelReady === false && modelProblem && (
        <p className="mb-4 max-w-md text-center text-[13px] leading-relaxed text-text-muted">
          <WithCode text={modelProblem} />{' '}
          <Link href={MODELS_SETTINGS} className="whitespace-nowrap font-medium text-accent underline-offset-2 hover:underline">
            Open Settings → Models
          </Link>
        </p>
      )}
      <h2 className="text-3xl md:text-4xl font-bold text-zinc-100 tracking-tight mb-1 text-center">
        What can I help you Achieve?
      </h2>
      <p className="text-text-faint text-sm mb-10 text-center font-mono">
        Synapse AI — Agents that work together to get things done
      </p>

      {/* Tips Carousel */}
      <div
        className="w-full max-w-xl mb-10"
        onMouseEnter={() => setIsPaused(true)}
        onMouseLeave={() => setIsPaused(false)}
      >
        {/* Card */}
        <div
          key={activeIdx}
          className={`tip-card-animate relative overflow-hidden border rounded-sm p-5 bg-gradient-to-br ${TIPS[activeIdx].accent}`}
          style={{ minHeight: 108 }}
        >
          <div className="flex items-start gap-3">
            <div className="mt-0.5 shrink-0">{TIPS[activeIdx].icon}</div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1.5">
                <span className={`text-[10px] font-mono uppercase tracking-widest px-2 py-0.5 rounded-sm border ${TIPS[activeIdx].tagColor}`}>
                  {TIPS[activeIdx].tag}
                </span>
              </div>
              <div className="text-[14px] font-semibold text-zinc-100 mb-1 leading-snug">
                {TIPS[activeIdx].title}
              </div>
              <p className="text-[12.5px] text-zinc-400 leading-relaxed">
                {TIPS[activeIdx].desc}
              </p>
            </div>
          </div>
        </div>

        {/* Dot indicators */}
        <div className="flex items-center justify-center gap-1.5 mt-3">
          {TIPS.map((tip, i) => (
            <button
              key={i}
              aria-label={`Show tip ${i + 1}: ${tip.title}`}
              aria-current={i === activeIdx}
              onClick={() => { setActiveIdx(i); setIsPaused(true); setTimeout(() => setIsPaused(false), 6000); }}
              className={`transition-all duration-300 rounded-full cursor-pointer ${i === activeIdx ? 'w-5 h-1.5 bg-zinc-300' : 'w-1.5 h-1.5 bg-zinc-700 hover:bg-accent-hover0'}`}
            />
          ))}
        </div>
      </div>

      {/* Quick action pills — navigate to settings */}
      <div className="flex flex-wrap gap-2 justify-center max-w-lg">
        {PILLS.map(({ label, path, icon: Icon }) => (
          <button
            key={label}
            onClick={() => onNavigate(path)}
            className="flex items-center gap-1.5 rounded-md border border-border bg-surface/70 px-3 py-1.5 text-sm text-text-muted transition-all hover:border-border-strong hover:bg-surface-2/80 hover:text-text cursor-pointer"
          >
            <Icon className="h-3.5 w-3.5" />
            {label}
          </button>
        ))}
      </div>
    </div>
  );
}

function generateSessionId() {
  const c = (globalThis as any).crypto;
  return c?.randomUUID?.() ?? `sess_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

export default function Home() {
  const [sessionId, setSessionId] = useState(() => {
    if (typeof window !== 'undefined') {
      return localStorage.getItem('synapseSessionId') || generateSessionId();
    }
    return generateSessionId();
  });

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [agentName, setAgentName] = useState('Loading...');
  const router = useRouter();
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [streamingActivity, setStreamingActivity] = useState<string | null>(null);
  const [isThinking, setIsThinking] = useState(false);
  // First line of the model's latest reasoning/progress message — shown next
  // to the 💭 indicator so "Thinking" is never a blank box.
  const [thinkingPreview, setThinkingPreview] = useState<string | null>(null);
  const [currentAgentId, setCurrentAgentId] = useState<string | null>(null);
  const [isHistoryOpen, setIsHistoryOpen] = useState(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Shared with the AI builder's composer — one validator, one chip strip, one
  // counter, one set of refusal messages.
  const attachments = useImageAttachments();
  const attachedImages = attachments.images;
  // Whether attaching anything to this agent does something. Answered by
  // `/api/status`, which already resolves each agent's model — the alternative
  // was a second copy in here of the list of providers that cannot see, and a
  // silent drop every time the two disagreed.
  const canAttach = (() => {
    const info = currentAgentId ? systemStatus?.agents?.[currentAgentId] : undefined;
    return typeof info === 'object' && info !== null && 'can_see_images' in info
      ? (info as { can_see_images?: boolean }).can_see_images !== false
      : true;
  })();

  // Welcome banner — shown for 12 days from installation date, dismissible per session
  const [showExamplesBanner, setShowExamplesBanner] = useState(false);

  // Accumulate LLM thoughts per active step during streaming
  const pendingThoughtsRef = useRef<string[]>([]);
  const pendingReasoningRef = useRef<string[]>([]);
  // Tool currently shown in the loading indicator. We keep the last tool's label
  // visible until a genuinely NEW tool starts (tool_execution). Without this, a
  // delegate agent's wrapper `delegate_to_agent` tool_result would clobber the
  // sub-agent's last real tool the instant the sub-agent returns.
  const lastToolRef = useRef<string | null>(null);
  // SSE abort controller — cancelled on new chat, unmount, or confirmed Settings navigation
  const sseAbortRef = useRef<AbortController | null>(null);
  // True while a chat/SSE request is in flight — gates the wake/network
  // recovery listeners so they don't abort an already-finished stream.
  const streamActiveRef = useRef(false);
  // Timestamp of the last byte received on the active stream. Used to tell a
  // healthy stream (recent heartbeat) from a dead one after sleep/network loss.
  const lastChunkAtRef = useRef(0);
  // The cursor (`id:`) of the last event this page handled. A turn runs on the
  // server whether or not anyone is reading (F49); when the stream carrying it
  // breaks, the page reads it again from here instead of sending it again.
  const lastCursorRef = useRef<string | null>(null);
  // Persists across SSE calls: true once orchestration_complete is received
  const orchestrationCompletedRef = useRef(false);

  // How the last turn ended, when it did not end with an answer. Drives the
  // retry bar: a turn that broke is the one case where the person is left with
  // nothing to do and no way to try again, which until now meant retyping.
  // `model`: the model failed rather than the app (F182), so the bar points at
  // Settings → Models as well as offering the turn again.
  const [turnOutcome, setTurnOutcome] = useState<null | { kind: 'failed' | 'cancelled'; message?: string; model?: boolean }>(null);
  // What to send if they do. Held in a ref rather than state because the retry
  // reads it from inside a handler, not during a render.
  const lastSentRef = useRef<{ content: string; images?: string[] } | null>(null);
  // Whether this turn actually produced an answer. A stream can end without
  // one — a proxy timing out mid-turn, a worker recycled — and that used to
  // look exactly like a finished conversation: the transcript simply stopped.
  const turnAnsweredRef = useRef(false);
  // Index of the user message being rewritten, or null. Editing one drops it
  // and everything after it — a conversation cannot fork.
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // Save current session to localStorage whenever it changes
  useEffect(() => {
    if (typeof window !== 'undefined') {
      localStorage.setItem('synapseSessionId', sessionId);
      if (currentAgentId) localStorage.setItem('synapseAgentId', currentAgentId);
    }
  }, [sessionId, currentAgentId]);

  // Leaving closes this page's stream and nothing else. The turn runs on the
  // server whether or not anyone is reading, its answer is saved to the
  // conversation, and coming back reopens it (F49). There used to be a confirm
  // here — "Navigating away will cancel it" — and leaving did cancel it; Stop is
  // how a turn is stopped now.
  useEffect(() => {
    return () => { sseAbortRef.current?.abort('leave'); };
  }, []);

  // Wake / network recovery: when the network comes back or the tab is
  // refocused, a stream that was alive before the machine slept is usually a
  // dead TCP socket that will never deliver another byte. If the active stream
  // has been silent for several heartbeat windows, abort it with a 'recover'
  // reason so the turn is read again from its cursor instead of hanging. A recent
  // chunk (heartbeat within 60s — 6 missed pings) means the stream is just slow
  // but alive, so leave it alone; plain tab switches and slow LLM turns don't
  // kill a working stream.
  useEffect(() => {
    const maybeRecover = () => {
      if (!streamActiveRef.current || !sseAbortRef.current) return;
      if (document.visibilityState === 'hidden') return;
      if (Date.now() - lastChunkAtRef.current <= 60000) return;
      setStreamingActivity('Connection lost — reconnecting…');
      sseAbortRef.current.abort('recover');
    };
    window.addEventListener('online', maybeRecover);
    document.addEventListener('visibilitychange', maybeRecover);
    return () => {
      window.removeEventListener('online', maybeRecover);
      document.removeEventListener('visibilitychange', maybeRecover);
    };
  }, []);



  // Fetch sessions list
  const fetchSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const res = await fetch('/api/sessions');
      if (res.ok) setSessions(await res.json());
    } catch (e) { console.error('[Sessions] fetch error', e); }
    finally { setSessionsLoading(false); }
  }, []);

  /**
   * Put a stored conversation back on the screen — all of what is open in it.
   *
   * Its transcript, a tool call it stopped to ask about (as the card, answerable
   * here), and a turn still being written: a turn is saved when it ends, so one
   * running now is not in the transcript yet — its question is shown and the
   * turn is read from its start (F49). Used when the page opens on the last
   * conversation and when one is picked from History.
   */
  const reopenConversation = async (sid: string, agentId: string | null) => {
    const running = await loadConversation(sid, agentId);
    if (running) await watchTurn(sid, running.cursor, running.message);
  };

  /** Draw the conversation as stored; the turn still being written, if any, is returned to be followed. */
  const loadConversation = async (sid: string, agentId: string | null): Promise<RunningTurn | null> => {
    try {
      const url = `/api/sessions/${encodeURIComponent(sid)}/history${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ''}`;
      const res = await fetch(url);
      if (!res.ok) return null;
      const data = await res.json();
      const turns: SessionTurn[] = data.turns || [];
      const parked: ParkedApproval | null = data.pending_approval ?? null;
      const running: RunningTurn | null = data.running ?? null;
      if (turns.length === 0 && !parked && !running) return null;

      const restored: Message[] = [];
      for (const t of turns) {
        restored.push({ role: 'user', content: t.user });
        restored.push({ role: 'assistant', content: t.assistant });
      }
      if (parked) {
        restored.push({ role: 'user', content: parked.message });
        restored.push({
          role: 'assistant', content: parked.prompt, msgType: 'human_input_required',
          data: { kind: 'tool_approval', tool_name: parked.tool_name, tool_args: parked.tool_args, category: parked.category },
        });
      }
      if (running) restored.push({ role: 'user', content: running.message });
      setMessages(restored);
      setTurnOutcome(null);
      setSessionId(sid);
      if (agentId) setCurrentAgentId(agentId);
      return running;
    } catch (e) {
      console.error('[Sessions] reopen error', e);
      return null;
    }
  };

  // Helper to refresh status
  const refreshSystemStatus = () => {
    fetch('/api/status').then(r => r.json()).then(d => {
      setSystemStatus(d);
      // Sync agent name with active agent from status to fix initial render mismatch
      if (d.active_agent_id && d.agents && d.agents[d.active_agent_id]) {
        const info = d.agents[d.active_agent_id];
        const name = typeof info === 'string' ? d.active_agent_id : info.name;
        setAgentName(name);
        setCurrentAgentId(d.active_agent_id);
      }
    }).catch(console.error);
  };

  // Initialize Application State — the last agent, and the conversation this
  // page was on. It used to open empty while keeping the conversation's id, so
  // the next message continued a conversation nobody could see; and a turn left
  // running when the page went away had nowhere to be seen (F49). New chat is
  // still one click.
  useEffect(() => {
    // 1. Get Agent Name + check installation date for banner
    fetch('/api/settings')
      .then(r => r.json())
      .then(d => {
        setAgentName(d.agent_name || 'System Agent');
        // Show example banner if installed_at exists and within 12 days
        if (d.installed_at) {
          const installedMs = new Date(d.installed_at).getTime();
          const twelvedays = 12 * 24 * 60 * 60 * 1000;
          if (Date.now() - installedMs < twelvedays) {
            setShowExamplesBanner(true);
          }
        } else {
          setShowExamplesBanner(true);
        }
      })
      .catch(() => setAgentName('Offline'));

    // 2. Restore last used agent, then its conversation
    const initAgent = async () => {
      const savedAgent = typeof window !== 'undefined' ? localStorage.getItem('synapseAgentId') : null;
      const savedSession = typeof window !== 'undefined' ? localStorage.getItem('synapseSessionId') : null;
      if (savedAgent) {
        try {
          await fetch('/api/agents/active', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_id: savedAgent }),
          });
        } catch (e) {
          console.error('Failed to restore last agent', e);
        }
      }
      refreshSystemStatus();
      if (savedSession) await reopenConversation(savedSession, savedAgent);
    };

    initAgent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Stop *reading* the turn on screen — never the turn.
   *
   * Starting a new chat, opening another conversation or switching agent moves
   * this page elsewhere; a turn still working carries on, its answer lands in
   * its own conversation, and History reopens it (F49 — the builder's rule
   * since D99). Stop is what stops a turn.
   */
  const stopReading = () => {
    sseAbortRef.current?.abort('leave');
    sseAbortRef.current = null;
    setIsLoading(false);
    setIsThinking(false);
    setStreamingActivity(null);
    setTurnOutcome(null);
    setEditingIndex(null);
  };

  const handleNewChat = () => {
    stopReading();
    setSessionId(generateSessionId());
    setMessages([]);
  };

  const handleRefresh = () => {
    refreshSystemStatus();
    if (isHistoryOpen) fetchSessions();
  };

  const handleOpenHistory = () => {
    setIsHistoryOpen(true);
    fetchSessions();
  };

  const handleSelectSession = async (s: SessionSummary) => {
    setIsHistoryOpen(false);
    stopReading();
    await reopenConversation(s.session_id, s.agent_id);
    // Switch agent if needed
    if (s.agent_id && s.agent_id !== currentAgentId) {
      try {
        await fetch('/api/agents/active', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ agent_id: s.agent_id }),
        });
        refreshSystemStatus();
      } catch { }
    }
  };

  const handleDeleteSession = async (e: React.MouseEvent, s: SessionSummary) => {
    e.stopPropagation();
    await fetch(`/api/sessions/${s.session_id}?agent_id=${s.agent_id}`, { method: 'DELETE' });
    setSessions(prev => prev.filter(x => x.session_id !== s.session_id));
  };

  const handleSwitchAgent = async (agentId: string) => {
    try {
      const res = await fetch('/api/agents/active', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: agentId })
      });
      if (res.ok) {
        // Refresh status immediately
        const statusRes = await fetch('/api/status');
        const statusData = await statusRes.json();
        setSystemStatus(statusData);
        const name = statusData.agents[agentId]?.name || agentId;
        if (statusData.agents[agentId]) {
          setAgentName(name);
        }
        setCurrentAgentId(agentId);

        // Generate new session for the new agent — isolates context. A turn the
        // old agent is still writing carries on in its own conversation.
        stopReading();
        setSessionId(generateSessionId());

        // Clear chat messages for clean agent context (no system bubble)
        setMessages([]);
      }
    } catch (e) {
      console.error("Failed to switch agent", e);
    }
  };



  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if ((!input.trim() && attachedImages.length === 0) || isLoading) return;

    // An image on its own is a message. It used to be sent as the literal
    // string "[Images attached]", which the model then read as the question.
    const userMessage = input.trim();
    const imagesToSend = attachedImages.map(img => img.base64);

    // Route to human-step resume if there's an unanswered human_input_required in history
    const lastHumanIdx = messages.findLastIndex(m => m.msgType === 'human_input_required');
    if (lastHumanIdx !== -1) {
      const hasReplyAfter = messages.slice(lastHumanIdx + 1).some(m => m.role === 'user');
      if (!hasReplyAfter) {
        const pendingStep = messages[lastHumanIdx];
        if (pendingStep.data?.run_id) {
          setInput('');
          if (textareaRef.current) textareaRef.current.style.height = 'auto';
          await handleHumanInputSubmit(pendingStep.data.run_id, pendingStep.data.orch_step_id, { response: userMessage });
          return;
        }
      }
    }

    setInput('');
    attachments.clear();
    if (textareaRef.current) textareaRef.current.style.height = 'auto';

    // Rewriting a question replaces it and drops everything after it. A
    // conversation cannot fork: the turns below were answers to the question
    // as it was asked, and keeping them would leave the model reading replies
    // to something nobody said.
    if (editingIndex !== null) {
      const original = messages[editingIndex];
      const isLastTurn = editingIndex === messages.length - 2;
      if (isLastTurn && original) await popStoredTurn(original.content);
      setMessages(prev => [
        ...prev.slice(0, editingIndex),
        { role: 'user', content: userMessage, images: imagesToSend.length > 0 ? imagesToSend : undefined },
      ]);
      setEditingIndex(null);
      await processMessage(userMessage, imagesToSend);
      return;
    }

    setMessages(prev => [...prev, { role: 'user', content: userMessage, images: imagesToSend.length > 0 ? imagesToSend : undefined }]);

    await processMessage(userMessage, imagesToSend);
  };

  /**
   * Stop the turn that is running.
   *
   * Two calls, because they do different jobs. The POST tells the backend to
   * stop working — without it a stopped turn goes on spending tokens against a
   * socket nobody is reading. The abort tells this browser to stop listening,
   * which is what makes the screen respond at once instead of when the server
   * gets around to it.
   *
   * `'stop'` as the abort reason matters: the catch in `processMessageSSE`
   * resolves on it rather than rejecting, so stopping does not fall through to
   * the HTTP path and quietly run the whole turn again.
   */
  const handleStop = async () => {
    if (!isLoading) return;
    setTurnOutcome({ kind: 'cancelled' });
    try {
      await fetch(`/api/chat/${encodeURIComponent(sessionId)}/cancel`, { method: 'POST' });
    } catch (e) {
      // The backend may be unreachable; the abort below still ends it here.
      console.error('Failed to reach the cancel endpoint', e);
    }
    sseAbortRef.current?.abort('stop');
    sseAbortRef.current = null;
  };

  /**
   * Take the last turn off the stored history, if it is the one we mean.
   *
   * Guarded, and the guard is load-bearing: a turn that *failed* was never
   * persisted, so the last stored turn is the previous, successful one.
   * Popping unconditionally would delete that.
   */
  const popStoredTurn = async (expectUser: string) => {
    try {
      await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/last-turn/pop`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ agent_id: currentAgentId, expect_user: expectUser }),
      });
    } catch (e) {
      console.error('Failed to take the last turn back', e);
    }
  };

  /** Run the last question again, in place of the answer that did not arrive. */
  const handleRetry = async () => {
    const payload = lastSentRef.current;
    if (!payload || isLoading) return;
    await popStoredTurn(payload.content);
    // Drop whatever the broken turn left in the transcript, back to and
    // excluding the question itself — that stays, and is asked again.
    setMessages(prev => {
      const idx = prev.findLastIndex(m => m.role === 'user');
      return idx === -1 ? prev : prev.slice(0, idx + 1);
    });
    await processMessage(payload.content, payload.images);
  };

  /** Put a question back in the composer to be rewritten. */
  const handleEditMessage = (index: number) => {
    const message = messages[index];
    if (!message || message.role !== 'user' || isLoading) return;
    setEditingIndex(index);
    setInput(message.content === '[Images attached]' ? '' : message.content);
    textareaRef.current?.focus();
  };

  /**
   * Read a turn's SSE stream through the shared event handler, keeping its cursor.
   *
   * The chat turn, a turn read again and the tool-approval resume all speak the
   * same event vocabulary, so the reading of it is written once. True when the
   * turn said `done`; false when the stream ended first — a proxy that gave up,
   * a socket that closed — which means read it again, not that it is over.
   */
  const consumeTurnStream = async (
    response: Response, controller: AbortController, stepState: StepState,
  ): Promise<boolean> => {
    const reader = response.body?.getReader();
    if (!reader) throw new Error('No reader available');
    const decoder = new TextDecoder();
    let buffer = '';
    let pendingId: string | null = null;
    while (true) {
      const { done, value } = await readWithStallTimeout(reader, controller);
      if (done) return false;
      lastChunkAtRef.current = Date.now();
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (line.startsWith('id: ')) { pendingId = line.slice(4); continue; }
        if (!line.startsWith('data: ')) continue;
        let data;
        try {
          data = JSON.parse(line.slice(6));
        } catch (e) {
          console.error('Failed to parse SSE event:', e);
          continue;
        }
        const result = handleSSEEvent(data, stepState);
        // The cursor moves only once its event has been handled, so reading
        // the turn again never skips the event a break interrupted.
        if (pendingId) { lastCursorRef.current = pendingId; pendingId = null; }
        if (stepState.orchCompleted) orchestrationCompletedRef.current = true;
        if (result.error) throw result.error;
        if (result.done) return true;
      }
    }
  };

  /**
   * Read the turn this conversation is running, from `cursor` on.
   *
   * 'done' when it ended; 'gone' when the server holds no turn to read — it
   * ended long enough ago that only its answer, in the conversation, is left;
   * 'stopped' when this page stopped reading (Stop, New chat, leaving).
   */
  const followTurn = async (sid: string, cursor: string | null): Promise<'done' | 'gone' | 'stopped'> => {
    const controller = new AbortController();
    sseAbortRef.current = controller;
    streamActiveRef.current = true;
    lastChunkAtRef.current = Date.now();
    try {
      const after = cursor ? `?after=${encodeURIComponent(cursor)}` : '';
      const response = await fetch(`/api/chat/${encodeURIComponent(sid)}/events${after}`, { signal: controller.signal });
      if (response.status === 404 || response.status === 410) return 'gone';
      if (!response.ok) throw new Error(`Reading the turn again failed: ${response.status}`);
      if (await consumeTurnStream(response, controller, newStepState())) return 'done';
      throw new Error('The stream ended before the turn did');
    } catch (e) {
      // An abort rejects with its *reason* ('stop', 'leave', 'recover'), not
      // an AbortError, so the signal is what says whether this page let go.
      if (controller.signal.aborted && controller.signal.reason !== 'recover') return 'stopped';
      throw e;
    } finally {
      streamActiveRef.current = false;
      if (sseAbortRef.current === controller) sseAbortRef.current = null;
    }
  };

  /** The turn ended while nobody was reading; its answer is in the conversation. */
  const showSavedAnswer = async (sid: string, question: string) => {
    try {
      const agent = currentAgentId ? `?agent_id=${encodeURIComponent(currentAgentId)}` : '';
      const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/history${agent}`);
      if (!res.ok) return;
      const turns: SessionTurn[] = (await res.json()).turns || [];
      const last = turns[turns.length - 1];
      if (last && last.user === question) {
        turnAnsweredRef.current = true;
        setMessages(prev => [...prev, { role: 'assistant', content: last.assistant }]);
      }
    } catch (e) {
      console.error('[SSE] reading the saved answer failed', e);
    }
  };

  /**
   * The stream carrying a turn broke — a reload, a laptop waking up, a proxy
   * that gave up. The turn did not: it runs on the server whether or not anyone
   * is reading. So it is read again from the last event handled, and never sent
   * again — the old fallback re-ran the whole turn over `POST /chat`, its tools
   * and all (F49). A few tries while the network comes back; after that the
   * retry bar, and the person decides.
   */
  const recoverTurn = async (sid: string, question: string) => {
    setStreamingActivity('Connection lost — reconnecting…');
    for (const wait of [0, 1000, 2000, 4000, 8000]) {
      if (wait) await new Promise(resolve => setTimeout(resolve, wait));
      try {
        const how = await followTurn(sid, lastCursorRef.current);
        if (how === 'gone') await showSavedAnswer(sid, question);
        return;
      } catch (e) {
        if ((e as { engineError?: boolean })?.engineError) throw e;
        console.error('[SSE] reading the turn again failed', e);
      }
    }
  };

  /**
   * Allow or refuse a tool call the agent stopped to ask about.
   *
   * The answer resumes the same turn — the backend picks it up from its last
   * turn-boundary snapshot — so this streams into the transcript exactly as
   * the first half did, rather than starting a new exchange.
   */
  const handleToolApproval = async (approve: boolean, reason: string) => {
    if (isLoading) return;
    // The question the card belongs to — what the turn will be saved under.
    const question = [...messages].reverse().find(m => m.role === 'user')?.content ?? '';
    setMessages(prev => [...prev, {
      role: 'assistant', msgType: 'note',
      content: approve ? 'You allowed the call.' : `You refused the call${reason ? `: ${reason}` : '.'}`,
    }]);
    setIsLoading(true);
    setTurnOutcome(null);
    turnAnsweredRef.current = false;
    lastCursorRef.current = null;
    const controller = new AbortController();
    sseAbortRef.current = controller;
    try {
      const response = await fetch(`/api/chat/${encodeURIComponent(sessionId)}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve, reason, agent_id: currentAgentId }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`Request failed: ${response.status}`);
      if (!(await consumeTurnStream(response, controller, newStepState()))) {
        await recoverTurn(sessionId, question);
      }
    } catch (e) {
      if ((e as { engineError?: boolean })?.engineError) {
        setTurnOutcome({
          kind: 'failed',
          message: (e as Error).message,
          model: Boolean((e as Error & { modelError?: boolean }).modelError),
        });
      } else if (!(controller.signal.aborted && controller.signal.reason !== 'recover')) {
        console.error('Lost the resumed turn — reading it again', e);
        await recoverTurn(sessionId, question);
      }
    } finally {
      if (sseAbortRef.current === controller) sseAbortRef.current = null;
      setIsLoading(false);
      setStreamingActivity(null);
      setIsThinking(false);
      setThinkingPreview(null);
    }
  };

  const handleCollectDataSubmit = async (values: Record<string, any>) => {
    // Format the response based on whether it's a single field or multiple fields
    const keys = Object.keys(values);

    let userMessage: string;
    if (keys.length === 1) {
      // Single field: just send the value
      const value = values[keys[0]];
      userMessage = Array.isArray(value) ? value.join(', ') : String(value);
    } else {
      // Multiple fields: format as "Field1: value1, Field2: value2"
      userMessage = keys
        .map(key => {
          const value = values[key];
          const displayValue = Array.isArray(value) ? value.join(', ') : value;
          return `${key}: ${displayValue}`;
        })
        .join(', ');
    }

    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    await processMessage(userMessage);
  };

  /**
   * One turn on screen, from the spinner to the retry bar.
   *
   * Shared by a turn this page sends and one it finds running when it reopens
   * a conversation, so both end the same way.
   */
  const inTurn = async (question: string, images: string[] | undefined, work: () => Promise<void>) => {
    lastSentRef.current = { content: question, images };
    turnAnsweredRef.current = false;
    lastCursorRef.current = null;
    setTurnOutcome(null);
    setIsLoading(true);
    setStreamingActivity(null);
    setIsThinking(false);
    setThinkingPreview(null);
    pendingThoughtsRef.current = [];
    pendingReasoningRef.current = [];
    lastToolRef.current = null;
    try {
      await work();
    } catch (error) {
      // An `error` event is the engine telling us the turn failed. Running it
      // again does not fix that — Retry is there, and the person decides.
      console.error('[SSE] the turn failed:', error);
      setTurnOutcome({
        kind: 'failed',
        message: (error as Error).message,
        model: Boolean((error as Error & { modelError?: boolean }).modelError),
      });
    } finally {
      // A turn that produced nothing and said nothing about why. The functional
      // setter is deliberate: an outcome already recorded — a stop, an engine
      // error — is the more specific answer and must not be overwritten by
      // this catch-all, and reading the state directly here would read the
      // value from before this turn started.
      if (!turnAnsweredRef.current) {
        setTurnOutcome(prev => prev ?? {
          kind: 'failed',
          message: 'The connection to this turn ended before it finished.',
        });
      }
      setIsLoading(false);
      setStreamingActivity(null);
      setIsThinking(false);
      setThinkingPreview(null);
      pendingThoughtsRef.current = [];
      pendingReasoningRef.current = [];
      // Reset session after orchestration so next query starts fresh (no sub-agent history)
      if (orchestrationCompletedRef.current) {
        orchestrationCompletedRef.current = false;
        setSessionId(generateSessionId());
      }
    }
  };

  const processMessage = (content: string, images?: string[]) => inTurn(content, images, async () => {
    try {
      await processMessageSSE(content, images);
    } catch (sseError) {
      if ((sseError as { engineError?: boolean })?.engineError) throw sseError;
      if ((sseError as { turnRunning?: boolean })?.turnRunning) {
        // This conversation already has a turn being written — another tab, or
        // one this page lost track of. Show that one; the message just typed
        // goes back in the composer rather than being lost or sent over it.
        setMessages(prev => {
          const last = prev[prev.length - 1];
          return last?.role === 'user' && last.content === content ? prev.slice(0, -1) : prev;
        });
        setInput(content);
        const running = await loadConversation(sessionId, currentAgentId);
        if (running) {
          lastSentRef.current = { content: running.message };
          const how = await followTurn(sessionId, running.cursor);
          if (how === 'gone') await showSavedAnswer(sessionId, running.message);
        } else {
          turnAnsweredRef.current = true;  // nothing was sent; nothing to retry
        }
        return;
      }
      await recoverTurn(sessionId, content);
    }
  });

  /** Show a turn this page did not start — found running when a conversation reopened. */
  const watchTurn = (sid: string, cursor: string, question: string) => inTurn(question, undefined, async () => {
    try {
      const how = await followTurn(sid, cursor);
      if (how === 'gone') await showSavedAnswer(sid, question);
    } catch (e) {
      if ((e as { engineError?: boolean })?.engineError) throw e;
      await recoverTurn(sid, question);
    }
  });

  // ── Shared SSE event processor ────────────────────────────────────────────
  // Handles every event type emitted by /api/chat/stream and the orchestration
  // resume endpoint. Mutates stepState in place for per-stream thought tracking.
  const handleSSEEvent = (
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    data: Record<string, any>,
    stepState: StepState
  ): { done: boolean; error?: Error } => {
    switch (data.type) {

      // ── Standard events ────────────────────────────────────────────────────
      case 'status':
        setStreamingActivity(data.message);
        setIsThinking(false);
        break;

      case 'thinking':
        setIsThinking(true);
        // Progress messages ("Analyzing your request…", "Delegating to X…")
        // used to be discarded — surface them in the thinking line.
        if (data.message) setThinkingPreview(`${delegatePrefix(data)}${String(data.message)}`);
        break;

      case 'tool_execution': {
        lastToolRef.current = data.tool_name as string;
        const toolDisplayName = (data.tool_name as string)
          .replace(/_/g, ' ')
          .replace(/\b\w/g, (l: string) => l.toUpperCase());
        const stepLabel = data.step_name ? ` · ${data.step_name}` : '';
        setStreamingActivity(`🔧 ${delegatePrefix(data)}${toolDisplayName}${stepLabel}`);
        setIsThinking(false);
        break;
      }

      case 'tool_result': {
        const rawToolName = data.tool_name as string || 'Tool';
        // Only update the indicator if this result is for the tool currently
        // shown. A wrapper tool finishing (e.g. delegate_to_agent) after a
        // sub-agent ran its own tools must NOT overwrite the sub-agent's last
        // activity — keep the last tool until a new tool is called.
        if (rawToolName !== lastToolRef.current) {
          setIsThinking(false);
          break;
        }
        const resultToolName = rawToolName
          .replace(/_/g, ' ')
          .replace(/\b\w/g, (l: string) => l.toUpperCase());
        const subtaskSuffix = data.subtask_name ? ` › ${data.subtask_name}` : '';
        setStreamingActivity(`✓ ${delegatePrefix(data)}${resultToolName}${subtaskSuffix}`);
        setIsThinking(false);
        break;
      }

      case 'llm_reasoning':
        // Private chain-of-thought from a [REASONING] block. Accumulates per
        // turn; rendered as the "Thinking" panel above the assistant bubble.
        if (data.orch_step_id) {
          if (data.orch_step_id !== stepState.orchStepId) {
            stepState.thoughts = [];
            stepState.reasoning = [];
            stepState.orchStepId = data.orch_step_id as string;
          }
          stepState.reasoning = [...stepState.reasoning, data.reasoning as string];
        } else {
          pendingReasoningRef.current = [...pendingReasoningRef.current, data.reasoning as string];
        }
        if (data.reasoning) {
          setThinkingPreview(
            `${delegatePrefix(data)}${String(data.reasoning).split('\n')[0].slice(0, 140)}`);
        }
        setIsThinking(true);
        break;

      case 'llm_thought':
        if (data.orch_step_id) {
          if (data.orch_step_id !== stepState.orchStepId) {
            stepState.thoughts = [];
            stepState.reasoning = [];
            stepState.orchStepId = data.orch_step_id as string;
          }
          stepState.thoughts = [...stepState.thoughts, data.thought as string];
        } else {
          pendingThoughtsRef.current = [...pendingThoughtsRef.current, data.thought as string];
        }
        setIsThinking(true);
        break;

      case 'response': {
        turnAnsweredRef.current = true;
        const capturedThoughts = withoutEcho([...pendingThoughtsRef.current], data.content);
        const capturedReasoning = [...pendingReasoningRef.current];
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: data.content as string,
          intent: data.intent,
          data: data.data,
          tool: data.tool_name,
          thoughts: capturedThoughts,
          reasoning: capturedReasoning,
        }]);
        pendingThoughtsRef.current = [];
        pendingReasoningRef.current = [];
        break;
      }

      // Raw 'final' events — emitted by resume/direct-run endpoints (no SSE transformation)
      case 'final': {
        if (data.orch_step_id) {
          // Sub-agent step inside an orchestration — render as agent_step_result bubble
          const capturedThoughts = withoutEcho([...stepState.thoughts], data.response);
          const capturedReasoning = [...stepState.reasoning];
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: data.response as string,
            intent: data.intent,
            data: data.data,
            tool: data.tool_name,
            msgType: 'agent_step_result',
            stepName: data.step_name,
            orchStepId: data.orch_step_id,
            thoughts: capturedThoughts,
            reasoning: capturedReasoning,
          }]);
          stepState.thoughts = [];
          stepState.reasoning = [];
          stepState.receivedAgentStep = true;
        } else {
          // Top-level final (single-agent response outside orchestration)
          const capturedThoughts = withoutEcho([...pendingThoughtsRef.current], data.response);
          const capturedReasoning = [...pendingReasoningRef.current];
          setMessages(prev => [...prev, {
            role: 'assistant',
            content: data.response as string,
            intent: data.intent,
            data: data.data,
            tool: data.tool_name,
            thoughts: capturedThoughts,
            reasoning: capturedReasoning,
          }]);
          pendingThoughtsRef.current = [];
          pendingReasoningRef.current = [];
        }
        break;
      }

      // ── Orchestration lifecycle ────────────────────────────────────────────
      case 'orchestration_start':
        setStreamingActivity(`🚀 Starting orchestration`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `🚀 ${(data.orchestration_name as string) || 'Orchestration'} started`,
          msgType: 'orchestration_start',
        }]);
        break;

      case 'step_start':
        stepState.thoughts = [];
        stepState.reasoning = [];
        stepState.orchStepId = data.orch_step_id as string;
        setStreamingActivity(`▶ ${(data.step_name as string) || 'Step'}`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: (data.step_name as string) || 'Step',
          msgType: 'step_start',
          stepName: data.step_name,
          stepType: data.step_type,
          orchStepId: data.orch_step_id,
        }]);
        break;

      case 'agent_step_result':
        stepState.receivedAgentStep = true;
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: data.content as string,
          intent: data.intent,
          data: data.data,
          tool: data.tool_name,
          msgType: 'agent_step_result',
          stepName: data.step_name,
          orchStepId: data.orch_step_id,
          thoughts: withoutEcho([...stepState.thoughts], data.content),
          reasoning: [...stepState.reasoning],
        }]);
        stepState.thoughts = [];
        stepState.reasoning = [];
        break;

      case 'step_complete':
        setStreamingActivity(`✓ ${(data.step_name as string) || 'Step'} complete`);
        break;

      case 'step_error':
        setStreamingActivity(`✗ Step failed`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `❌ Step failed: ${(data.error as string) || 'Unknown error'}`,
          msgType: 'orchestration_complete',
        }]);
        break;

      case 'orchestration_complete':
        setStreamingActivity(`Orchestration ${data.status as string}`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ Orchestration ${(data.status as string) || 'complete'}`,
          msgType: 'orchestration_complete',
        }]);
        // Fallback: if no agent_step_result events arrived, display final_state values
        if (!stepState.receivedAgentStep && data.final_state) {
          Object.entries(data.final_state as Record<string, unknown>).forEach(([key, value]) => {
            if (typeof value === 'string' && value.trim()) {
              setMessages(prev => [...prev, {
                role: 'assistant',
                content: value,
                msgType: 'agent_step_result',
                stepName: key,
                thoughts: [],
              }]);
            }
          });
        }
        stepState.orchCompleted = true;
        break;

      case 'orchestration_error':
        setStreamingActivity(`Orchestration error`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `❌ Orchestration error: ${(data.error as string) || 'Unknown error'}`,
          msgType: 'orchestration_complete',
        }]);
        break;

      case 'human_input_required':
        // A pause is how this turn ends: the stream closes on purpose, and
        // the turn goes on once it is answered. Read as unanswered, the close
        // drew "The connection to this turn ended — Try again" under the
        // question (F209), offering to re-run a turn that was only waiting.
        turnAnsweredRef.current = true;
        setStreamingActivity(null);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: (data.prompt as string) || (data.content as string) || 'Please provide input to continue.',
          msgType: 'human_input_required',
          data: {
            fields: data.fields,
            run_id: data.run_id,
            orch_step_id: data.orch_step_id,
            agent_context: data.agent_context,
            channel_id: data.channel_id,
            // A gated tool call, rather than a workflow asking a question. It
            // has no run behind it — the turn itself is what resumes — so it
            // is answered somewhere else and rendered as a decision, not a form.
            kind: data.kind,
            tool_name: data.tool_name,
            tool_args: data.tool_args,
            category: data.category,
          },
        }]);
        break;

      // ── Parallel / loop / routing info ──────────────────────────────────────
      case 'evaluator_decision':
        setStreamingActivity(
          `🔀 Route: ${String(data.tool_name || '').replace(/^route_/, '') || '(no route)'}`);
        break;

      case 'routing_decision': {
        // The chat stream's allow-list already passed this event for evaluator
        // steps; if/switch routing reaches it now too, so a switch taking its
        // default must not render as `Route: undefined`.
        const st = data.step_type as string | undefined;
        setStreamingActivity(
          st === 'if_else' ? `🔀 Branch: ${(data.decision as string) ?? 'false'}`
            : st === 'switch' ? `🔀 Case: ${(data.decision as string) ?? 'default'}`
              : `🔀 Route: ${(data.decision as string) || (data.tool_name as string)}`);
        break;
      }

      case 'loop_iteration':
        setStreamingActivity(`🔄 Loop iteration ${data.iteration}/${data.total}`);
        break;

      case 'parallel_start':
        setStreamingActivity(`⚡ Running ${data.branch_count} parallel branches`);
        break;

      case 'branch_start':
        setStreamingActivity(`⚡ Branch ${((data.branch_index as number) ?? 0) + 1}/${data.branch_count}`);
        break;

      case 'merge_complete':
        setStreamingActivity(`🔗 Merged ${data.input_count} results`);
        break;

      // ── What the loop did to keep going ──────────────────────────────────
      // All four are emitted by the engine and were dropped at the SSE route,
      // so the only trace of a truncated answer or a discarded middle was that
      // the reply read oddly. They are notes, not messages: nobody said them.
      case 'truncated_output':
        setMessages(prev => [...prev, {
          role: 'assistant', msgType: 'note',
          content: 'That answer hit the response limit. The agent was asked to carry on from where it stopped.',
        }]);
        return { done: false };

      case 'context_trimmed':
        setMessages(prev => [...prev, {
          role: 'assistant', msgType: 'note',
          content: `The conversation outgrew the model's context, so the middle of it was dropped${
            data.chars_dropped ? ` (${Math.round((data.chars_dropped as number) / 1000)}k characters)` : ''
          }. The start and the most recent turns are intact.`,
        }]);
        return { done: false };

      case 'tool_call_unparsed':
        setMessages(prev => [...prev, {
          role: 'assistant', msgType: 'note',
          content: 'The agent wrote a tool call that could not be read, and was asked for it again.',
        }]);
        return { done: false };

      case 'tool_approval_replay':
        setMessages(prev => [...prev, {
          role: 'assistant', msgType: 'note',
          content: `Running ${data.tool_name as string} — the call you approved.`,
        }]);
        return { done: false };

      case 'done':
        return { done: true };

      case 'error': {
        const err = new Error(data.message as string) as Error & { engineError?: boolean; modelError?: boolean };
        err.engineError = true;
        err.modelError = data.kind === 'model';
        return { done: false, error: err };
      }

      case 'cancelled':
        // The turn was stopped — by this browser or another one on the same
        // session. Either way it ended on purpose, so it is not an error.
        setTurnOutcome({ kind: 'cancelled' });
        return { done: false };
    }
    return { done: false };
  };

  // SSE Streaming implementation
  const processMessageSSE = async (content: string, images?: string[]) => {
    const controller = new AbortController();
    sseAbortRef.current = controller;
    streamActiveRef.current = true;
    lastChunkAtRef.current = Date.now();
    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: content, session_id: sessionId, agent_id: currentAgentId, images: images || [] }),
        signal: controller.signal,
      });
      if (response.status === 409) {
        throw Object.assign(new Error('A reply is still being written in this conversation.'), { turnRunning: true });
      }
      if (!response.ok) throw new Error(`SSE request failed: ${response.status}`);
      if (!(await consumeTurnStream(response, controller, newStepState()))) {
        throw new Error('The stream ended before the turn did');
      }
    } catch (err) {
      // An abort rejects with its *reason*, not an AbortError. Stop, New chat
      // and leaving let go on purpose; 'recover' (the tab woke on a dead
      // socket) is a broken stream like any other, and is read again.
      if (controller.signal.aborted && controller.signal.reason !== 'recover') return;
      throw err;
    } finally {
      streamActiveRef.current = false;
      if (sseAbortRef.current === controller) sseAbortRef.current = null;
    }
  };

  // Resume a paused orchestration after a HUMAN step
  const handleHumanInputSubmit = async (
    runId: string,
    stepId: string | undefined,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    values: Record<string, any>
  ) => {
    const keys = Object.keys(values);
    let userMessage: string;
    if (keys.length === 1) {
      const value = values[keys[0]];
      userMessage = Array.isArray(value) ? (value as string[]).join(', ') : String(value);
    } else {
      userMessage = keys
        .map(key => {
          const value = values[key];
          const displayValue = Array.isArray(value) ? (value as string[]).join(', ') : value;
          return `${key}: ${displayValue}`;
        })
        .join(', ');
    }

    setMessages(prev => [...prev, { role: 'user', content: userMessage }]);
    setIsLoading(true);
    setStreamingActivity('Resuming orchestration...');
    setIsThinking(false);
    pendingThoughtsRef.current = [];
    pendingReasoningRef.current = [];

    const controller = new AbortController();
    try {
      const response = await fetch(`/api/orchestrations/runs/${runId}/human-input`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ response: values, step_id: stepId || '' }),
        signal: controller.signal,
      });

      // Refused (F215): most often someone else answered first. Say why,
      // rather than the generic error below.
      if (!response.ok) {
        const reason = await refusedAnswerMessage(response);
        setMessages(prev => [...prev, { role: 'assistant', content: reason }]);
        return;
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error('No reader available');

      const decoder = new TextDecoder();
      const stepState = { thoughts: [] as string[], reasoning: [] as string[], orchStepId: null as string | null, orchCompleted: false, receivedAgentStep: false };
      let buffer = '';

      while (true) {
        const { done, value } = await readWithStallTimeout(reader, controller);
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              const result = handleSSEEvent(data, stepState);
              if (result.done || result.error) return;
            } catch (e) {
              console.error('Failed to parse human-input SSE event:', e);
            }
          }
        }
      }
    } catch (err) {
      console.error('[HumanInput] Resume failed:', err);
      setMessages(prev => [...prev, { role: 'assistant', content: 'Error resuming orchestration.' }]);
    } finally {
      setIsLoading(false);
      setStreamingActivity(null);
      setIsThinking(false);
      pendingThoughtsRef.current = [];
      pendingReasoningRef.current = [];
    }
  };

  // ─── Rendering helpers ─────────────────────────────────────────────────────

  const renderMessage = (msg: Message, idx: number) => {
    // ── Orchestration start / complete banners ──
    if (msg.msgType === 'orchestration_start') {
      return (
        <div key={idx}>
          <OrchBanner content={msg.content} variant="start" />
        </div>
      );
    }
    if (msg.msgType === 'orchestration_complete') {
      const isError = msg.content.startsWith('❌');
      return (
        <div key={idx}>
          <OrchBanner content={msg.content} variant={isError ? 'error' : 'complete'} />
        </div>
      );
    }

    // ── Step divider line ──
    if (msg.msgType === 'step_start') {
      return (
        <div key={idx}>
          <StepDivider stepName={msg.stepName || msg.content} stepType={msg.stepType} />
        </div>
      );
    }

    // ── Sub-agent result (purple accent bubble) ──
    if (msg.msgType === 'agent_step_result') {
      return (
        <AgentStepResult
          key={idx}
          msg={msg}
          onCollectDataSubmit={handleCollectDataSubmit}
        />
      );
    }

    // ── A quiet line: something the loop did, not something anyone said ──
    if (msg.msgType === 'note') {
      return (
        <div key={idx} className="flex items-center gap-2 max-w-4xl py-0.5 pl-11 text-[11px] text-text-faint font-sans">
          <span className="h-px w-4 bg-zinc-800 shrink-0" aria-hidden />
          <span className="min-w-0">{msg.content}</span>
        </div>
      );
    }

    // ── A tool call waiting on permission ──
    // Not a form: the answer is yes or no, and the interesting part is *what*
    // is being asked, so the call and its arguments are the content.
    if (msg.msgType === 'human_input_required' && msg.data?.kind === 'tool_approval') {
      return <ToolApprovalCard key={idx} msg={msg} onDecide={handleToolApproval} disabled={isLoading} />;
    }

    // ── Human input required ──
    if (msg.msgType === 'human_input_required') {
      return (
        <div key={idx} className="flex gap-4 max-w-4xl">
          <div className="h-8 w-8 shrink-0 flex items-center justify-center border border-amber-700/50 bg-amber-950/30 text-amber-400 rounded-md">
            <User className="h-4 w-4" />
          </div>
          <div className="flex flex-col flex-1 min-w-0 gap-3">
            <div className="p-4 text-[15px] leading-7 border border-amber-900/40 bg-amber-950/10 relative font-sans rounded-md">
              <div className="absolute -top-3 left-2 bg-zinc-950 border border-amber-900/50 px-2 py-0.5 text-[10px] uppercase tracking-wider text-amber-500 font-mono rounded-md">
                Human Input Required
              </div>
              <Markdown className="mb-4">{msg.content}</Markdown>
              {Array.isArray(msg.data?.fields) && msg.data.fields.length > 0 ? (
                <HumanStepAnswer
                  fields={msg.data.fields}
                  onSubmit={(answer) => handleHumanInputSubmit(msg.data?.run_id, msg.data?.orch_step_id, answer)}
                />
              ) : (
                <CollectDataForm
                  data={{ ...msg.data, fields: [] }}
                  onSubmit={(values) => handleHumanInputSubmit(msg.data?.run_id, msg.data?.orch_step_id, values)}
                />
              )}
              {msg.data?.channel_id && (
                <div className="mt-2 text-[11px] text-text-faint font-mono">
                  📲 Notification sent to messaging channel
                </div>
              )}
            </div>
          </div>
        </div>
      );
    }

    // ── Regular user / assistant message ──
    return (
      <div key={idx} className={cn(
        "flex gap-4",
        msg.role === 'assistant' ? "max-w-4xl" : "max-w-3xl",
        msg.role === 'user' ? "ml-auto flex-row-reverse" : ""
      )}>
        <div className={cn(
          "h-8 w-8 shrink-0 flex items-center justify-center border",
          msg.role === 'user' ? "bg-white border-white text-black" : "bg-black border-zinc-700 text-zinc-400"
        )}>
          {msg.role === 'user' ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
        </div>

        <div className={cn(
          "flex flex-col flex-1 min-w-0 gap-2",
          msg.role === 'assistant' && msg.intent ? "mt-2" : ""
        )}>
          {msg.role === 'user' ? (
            /* ── User bubble ── */
            <div className="group p-4 bg-zinc-900 border border-zinc-800 text-zinc-100 text-[15px] leading-7 relative font-sans self-end max-w-[80%] rounded-md">
              {/* Rewrite this question.

                  Kept out of the hover-only pattern that hides the image
                  remove button: this one is focusable, so it can be reached
                  without a pointer. It only shows on the *last* question —
                  editing an earlier one would drop every answer after it,
                  which is a bigger action than a pencil implies. */}
              {!isLoading && idx === messages.length - 2 && (
                <button
                  type="button"
                  onClick={() => handleEditMessage(idx)}
                  aria-label="Edit this message and send it again"
                  title="Edit and send again"
                  className={cn(
                    "absolute -left-9 top-3 p-1.5 rounded-md text-text-faint transition-all",
                    "opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
                    "hover:text-zinc-300 hover:bg-zinc-800",
                  )}
                >
                  <Pencil className="h-3.5 w-3.5" />
                </button>
              )}
              <Markdown>{msg.content}</Markdown>
              {msg.images && msg.images.length > 0 && (
                <div className="flex flex-wrap gap-3 mt-4">
                  {msg.images.map((img, imgIdx) => (
                    <div key={imgIdx} className="relative group">
                      <img
                        src={img}
                        alt={`Attached ${imgIdx + 1}`}
                        className="h-[4.5rem] w-[4.5rem] object-cover border border-zinc-700/60 rounded-xl cursor-pointer hover:border-zinc-400 hover:scale-[1.03] hover:shadow-lg transition-all"
                        onClick={() => window.open(img, '_blank')}
                      />
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <AssistantBubble msg={msg} agentName={agentName} />
          )}

          {/* Dynamic UI based on Intent - Rendered Outside Bubble */}
          {msg.role === 'assistant' && (
            <div className="w-full mt-2 pl-1">
              {msg.intent === 'render_local_file' && (
                <div className="mt-4 p-4 bg-zinc-950 border border-zinc-800 font-mono text-xs whitespace-pre-wrap max-h-96 overflow-auto text-zinc-300 rounded-md">
                  {msg.data.content}
                </div>
              )}
              {msg.intent === 'collect_data' && msg.data && (
                <CollectDataForm data={msg.data} onSubmit={handleCollectDataSubmit} />
              )}
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="flex h-full min-h-0 overflow-hidden font-mono">

      {/* ── History Drawer ────────────────────────────────────────────────── */}
      {isHistoryOpen && (
        <div className="fixed inset-0 z-50 flex">
          {/* Backdrop */}
          <div
            className="absolute inset-0 bg-scrim/60 backdrop-blur-sm cursor-pointer"
            onClick={() => setIsHistoryOpen(false)}
          />
          {/* Panel */}
          <div className="relative z-10 w-80 max-w-full h-full bg-zinc-950 border-r border-zinc-800 flex flex-col shadow-2xl">
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-zinc-800 shrink-0">
              <div className="flex items-center gap-2">
                <History className="h-4 w-4 text-zinc-400" />
                <span className="text-xs font-bold uppercase tracking-widest text-zinc-300">Chat History</span>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={fetchSessions}
                  disabled={sessionsLoading}
                  className="p-1.5 hover:bg-zinc-800 rounded-md text-zinc-400 hover:text-white transition-colors"
                  title="Refresh sessions"
                >
                  <RefreshCw className={cn("h-3.5 w-3.5", sessionsLoading && "animate-spin")} />
                </button>
                <button
                  onClick={() => setIsHistoryOpen(false)}
                  className="p-1.5 hover:bg-zinc-800 rounded-md text-zinc-400 hover:text-white transition-colors"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>

            {/* Session List */}
            <div className="flex-1 overflow-y-auto custom-scrollbar">
              {sessionsLoading && sessions.length === 0 && (
                <div className="flex items-center justify-center py-10 text-text-faint text-xs">Loading...</div>
              )}
              {!sessionsLoading && sessions.length === 0 && (
                <div className="flex flex-col items-center justify-center py-10 gap-2">
                  <Clock className="h-8 w-8 text-text-faint" />
                  <span className="text-xs text-text-faint">No chat history yet</span>
                </div>
              )}
              {sessions.map(s => (
                <div
                  key={`${s.agent_id}_${s.session_id}`}
                  onClick={() => handleSelectSession(s)}
                  className={cn(
                    "group relative px-4 py-3 border-b border-zinc-900 cursor-pointer hover:bg-zinc-900/60 transition-colors",
                    s.session_id === sessionId && "bg-zinc-900"
                  )}
                >
                  {/* Active indicator */}
                  {s.session_id === sessionId && (
                    <div className="absolute left-0 top-0 bottom-0 w-0.5 bg-white" />
                  )}
                  <div className="flex items-start justify-between gap-2 mb-1">
                    <span className="text-[10px] uppercase tracking-wider text-text-faint font-mono truncate">
                      {s.agent_id || 'default'}
                    </span>
                    <div className="flex items-center gap-1 shrink-0">
                      <span className="text-[10px] text-text-faint font-mono">
                        {formatRelativeTime(s.last_updated)}
                      </span>
                      <button
                        onClick={(e) => handleDeleteSession(e, s)}
                        className="opacity-0 group-hover:opacity-100 p-0.5 hover:text-red-400 text-text-faint transition-all"
                        title="Delete session"
                      >
                        <Trash2 className="h-3 w-3" />
                      </button>
                    </div>
                  </div>
                  <p className="text-[12px] text-zinc-300 leading-5 line-clamp-2">
                    {s.first_user_message || '(no messages)'}
                  </p>
                  {s.last_response && (
                    <p className="text-[11px] text-text-faint mt-1 truncate">
                      ↩ {s.last_response.slice(0, 80)}{s.last_response.length > 80 ? '…' : ''}
                    </p>
                  )}
                  <div className="text-[10px] text-text-faint mt-1 font-mono">
                    {s.turn_count} turn{s.turn_count !== 1 ? 's' : ''}
                  </div>
                </div>
              ))}
            </div>

            {/* Footer — new chat shortcut */}
            <div className="px-4 py-3 border-t border-zinc-800 shrink-0">
              <button
                onClick={() => { setIsHistoryOpen(false); handleNewChat(); }}
                className="w-full flex items-center justify-center gap-2 py-2 border border-zinc-700 hover:border-zinc-500 text-zinc-400 hover:text-white text-xs uppercase tracking-widest transition-colors"
              >
                <Plus className="h-3.5 w-3.5" />
                New Chat
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="flex-1 flex flex-col w-full border-r border-zinc-800 relative">
        {/* Header */}
        <header className="h-14 shrink-0 border-b border-border bg-surface px-3 md:px-6 z-10">
          <div className='w-full md:max-w-5xl mx-auto h-full flex items-center justify-between gap-2'>
            <div className="flex items-center gap-2.5 min-w-0">
              <div
                className={cn(
                  "size-2 shrink-0 rounded-full",
                  systemStatus?.model_ready === true ? "bg-success animate-pulse"
                    : systemStatus?.model_ready === false ? "bg-warning" : "bg-zinc-600",
                )}
                title={systemStatus?.model_problem || undefined}
              ></div>
              <h1
                className="min-w-0 truncate text-sm font-semibold tracking-tight text-text"
                title={agentName}
              >
                {agentName}
              </h1>
            </div>
            <div className="flex items-center">
              {/* Mode & Model Info */}
              <div className="hidden lg:flex items-center gap-4 text-xs text-text-muted uppercase tracking-wider border-r border-border pr-4">
                <div className="flex items-center gap-2">
                  <span className="text-zinc-400">Provider:</span>
                  <span className={cn("font-bold",
                    systemStatus?.provider === 'ollama' ? "text-green-400" :
                      systemStatus?.provider === 'gemini' ? "text-blue-400" :
                        systemStatus?.provider === 'anthropic' ? "text-amber-400" :
                          systemStatus?.provider === 'openai' ? "text-emerald-400" :
                            systemStatus?.provider === 'anthropic_cli' ? "text-amber-300" :
                              systemStatus?.provider === 'gemini_cli' ? "text-blue-300" :
                                systemStatus?.provider === 'codex_cli' ? "text-zinc-300" :
                                  "text-accent"
                  )}>
                    {systemStatus?.provider ? systemStatus.provider.charAt(0).toUpperCase() + systemStatus.provider.slice(1) : 'Loading...'}
                  </span>
                </div>
                <div className="flex items-center gap-2 min-w-0">
                  <span className="text-zinc-400 shrink-0">Model:</span>
                  <span
                    className="text-zinc-200 truncate max-w-[120px] lg:max-w-[180px]"
                    title={systemStatus?.model || undefined}
                  >
                    {systemStatus?.model || 'Loading...'}
                  </span>
                </div>
              </div>

              {/* Status Indicators */}
              {/* Agents Hover Status */}
              <div className="group relative hidden sm:flex items-center gap-2 cursor-pointer border-r border-border pl-4 pr-4 hover:bg-surface-2 transition-colors">
                <span className="text-xs font-bold text-zinc-400 tracking-widest uppercase group-hover:text-zinc-200 transition-colors">AGENTS</span>

                {/* Dropdown on Hover */}
                <div className="absolute right-0 top-full mt-0 w-64 bg-zinc-950 border border-zinc-800 p-2 shadow-2xl opacity-0 translate-y-2 group-hover:opacity-100 group-hover:translate-y-0 transition-all pointer-events-none group-hover:pointer-events-auto z-50 max-h-[60vh] overflow-y-auto custom-scrollbar rounded-md">
                  <div className="space-y-1">
                    {(() => {
                      // Hide native-builder machinery agents (ids prefixed `agent_native_builder`);
                      // show only user-facing agents.
                      const visibleAgents = systemStatus?.agents
                        ? Object.entries(systemStatus.agents).filter(([id]) => !id.startsWith('agent_native_builder'))
                        : [];

                      if (visibleAgents.length === 0) {
                        return <div className="text-xs text-text-faint italic px-3 py-2">No agents detected.</div>;
                      }

                      return visibleAgents.map(([id, info]) => {
                        const isActive = systemStatus?.active_agent_id === id;
                        // Handle legacy string vs new object structure if backend update lags (safety)
                        const name = typeof info === 'string' ? id : info.name;
                        const status = typeof info === 'string' ? info : info.status;

                        return (
                          <button
                            key={id}
                            onClick={() => handleSwitchAgent(id)}
                            className={cn(
                              "nav-button w-full flex items-center justify-between px-3 py-2 text-xs uppercase tracking-wider text-left border border-transparent hover:border-zinc-700 transition-all",
                              isActive ? "bg-zinc-900 text-white border-zinc-800" : "text-zinc-400 hover:text-zinc-200 hover:bg-zinc-900/50"
                            )}>
                            <div className="flex items-center gap-2">
                              <div className={cn("h-1.5 w-1.5 rounded-full", status === 'online' ? "bg-green-500 shadow-[0_0_5px_#22c55e]" : "bg-red-500")}></div>
                              <span className="truncate max-w-[200px]">{name}</span>
                            </div>
                            {isActive && <div className="h-1.5 w-1.5 bg-white rounded-full animate-pulse"></div>}
                          </button>
                        );
                      });
                    })()}
                  </div>
                </div>
              </div>

              <button
                onClick={handleNewChat}
                className="p-2 ml-2 hover:bg-zinc-900 rounded-md text-zinc-400 hover:text-white transition-colors"
                title="New Chat"
              >
                <Plus className="h-4 w-4" />
              </button>

              <button
                onClick={handleOpenHistory}
                className="p-2 hover:bg-zinc-900 rounded-md text-zinc-400 hover:text-white transition-colors"
                title="Chat History"
              >
                <History className="h-4 w-4" />
              </button>

            </div>
          </div>

        </header>

        {/* Running / paused orchestrations — click through to the live run view */}
        <ActiveRunsBanner />

        {/* Chat Area */}
        <div className="flex-1 overflow-y-auto scroll-smooth custom-scrollbar relative">

          {messages.length === 0 && !isLoading ? (
            /* ── Welcome Screen ── */
            <WelcomeScreen
              agentName={agentName}
              modelReady={systemStatus?.model_ready}
              modelProblem={systemStatus?.model_problem}
              onPrompt={(text) => { setInput(text); setTimeout(() => textareaRef.current?.focus(), 50); }}
              onNavigate={(path) => router.push(path)}
              showExamplesBanner={showExamplesBanner}
              onDismissBanner={() => setShowExamplesBanner(false)}
            />
          ) : (
            <div className="p-3 pb-32 md:p-6 md:pb-36">
              <div className="w-full md:max-w-5xl mx-auto space-y-6">
                {messages.map((msg, idx) => renderMessage(msg, idx))}

                {/* Loading Indicator */}
                {isLoading && (
                  <div className="flex gap-4 max-w-3xl items-start">
                    {/* The mark is the avatar while the agent is working: one
                        symbol carrying the motion, at the size the robot tile
                        used to be, rather than a bot inside a spinning ring. */}
                    <Spinner size={32} className="mt-0.5 shrink-0 text-accent" label="Working" />

                    <div className="flex flex-col gap-1">
                      <div className="flex items-baseline gap-0.5">
                        <span className="text-sm text-zinc-300 font-medium">
                          {streamingActivity || 'Processing'}
                        </span>
                        {!isThinking && (
                          <span className="flex gap-0.5 items-end pb-0.5 ml-0.5">
                            <span className="inline-block w-0.5 h-0.5 bg-accent rounded-full animate-bounce" style={{ animationDelay: '0ms', animationDuration: '1s' }}></span>
                            <span className="inline-block w-0.5 h-0.5 bg-accent rounded-full animate-bounce" style={{ animationDelay: '150ms', animationDuration: '1s' }}></span>
                            <span className="inline-block w-0.5 h-0.5 bg-accent rounded-full animate-bounce" style={{ animationDelay: '300ms', animationDuration: '1s' }}></span>
                          </span>
                        )}
                      </div>

                      {isThinking && (
                        <div className="flex items-baseline gap-0.5 max-w-2xl">
                          <span className="text-xs text-text-faint font-mono truncate">💭 {thinkingPreview || 'Thinking'}</span>
                          <span className="flex gap-0.5 items-end pb-0.5 ml-0.5">
                            <span className="inline-block w-0.5 h-0.5 bg-zinc-500 rounded-full animate-bounce" style={{ animationDelay: '0ms', animationDuration: '1s' }}></span>
                            <span className="inline-block w-0.5 h-0.5 bg-zinc-500 rounded-full animate-bounce" style={{ animationDelay: '150ms', animationDuration: '1s' }}></span>
                            <span className="inline-block w-0.5 h-0.5 bg-zinc-500 rounded-full animate-bounce" style={{ animationDelay: '300ms', animationDuration: '1s' }}></span>
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                )}

                <div ref={messagesEndRef} />
              </div>
            </div>
          )}
        </div>

        {/* Input Area */}
        <div className="absolute bottom-0 left-0 right-0 px-3 pb-3 pt-6 md:p-6 bg-gradient-to-t from-bg via-bg to-transparent">
          <div className="w-full md:max-w-5xl mx-auto">
            {/* The turn ended without an answer.

                Until now this was the end of the road: the transcript simply
                stopped, and trying again meant retyping the question. The bar
                says which of the two things happened, because they read very
                differently — one is the product failing, the other is the
                person deciding. */}
            {turnOutcome && !isLoading && (
              <div className={cn(
                // Wraps: at phone width the actions go under the sentence
                // rather than squeezing it to nothing.
                "flex flex-wrap items-center gap-x-3 gap-y-2 mb-3 px-3 py-2.5 border text-xs rounded-md",
                turnOutcome.kind === 'failed'
                  ? "border-red-900/50 bg-red-950/20 text-red-300"
                  : "border-zinc-800 bg-zinc-950/80 text-zinc-400",
              )}>
                {turnOutcome.kind === 'failed'
                  ? <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                  : <Square className="h-3 w-3 shrink-0 fill-current" />}
                {/* Clamped, not truncated: the provider's own words are the
                    useful part, and a one-line cut kept only the start. */}
                <span
                  className="min-w-[12rem] flex-1 font-sans break-words line-clamp-3"
                  title={turnOutcome.kind === 'failed' ? turnOutcome.message : undefined}
                >
                  {turnOutcome.kind === 'failed'
                    ? (turnOutcome.model
                      ? <><span className="font-semibold">The model couldn&apos;t answer.</span> {turnOutcome.message}</>
                      : (turnOutcome.message || 'That turn failed before it finished.'))
                    : 'Stopped.'}
                </span>
                <div className="ml-auto flex shrink-0 items-center gap-3">
                  {turnOutcome.kind === 'failed' && turnOutcome.model && (
                    <Link
                      href={MODELS_SETTINGS}
                      className="shrink-0 font-medium text-text underline underline-offset-2 hover:text-accent"
                    >
                      Settings → Models
                    </Link>
                  )}
                  <button
                    type="button"
                    onClick={handleRetry}
                    className={cn(
                      "shrink-0 flex items-center gap-1.5 px-2.5 py-1 rounded-md border transition-colors",
                      "border-border-strong text-text hover:bg-row-hover hover:border-accent",
                    )}
                  >
                    <RotateCcw className="h-3 w-3" />
                    Try again
                  </button>
                  <button
                    type="button"
                    onClick={() => setTurnOutcome(null)}
                    aria-label="Dismiss"
                    className="shrink-0 p-1 rounded-md text-text-faint hover:text-zinc-300 hover:bg-zinc-800 transition-colors"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              </div>
            )}

            {/* Rewriting a question, rather than asking a new one. */}
            {editingIndex !== null && (
              <div className="flex items-center gap-3 mb-3 px-3 py-2.5 border border-accent/40 bg-accent/5 text-xs text-text rounded-md">
                <Pencil className="h-3.5 w-3.5 shrink-0 text-accent" />
                <span className="min-w-0 flex-1 font-sans">
                  Editing your message. Sending replaces it and drops the replies below it.
                </span>
                <button
                  type="button"
                  onClick={() => { setEditingIndex(null); setInput(''); }}
                  className="shrink-0 px-2.5 py-1 rounded-md border border-border-strong text-text-muted hover:bg-row-hover hover:text-text transition-colors"
                >
                  Cancel
                </button>
              </div>
            )}

            <AttachmentError error={attachments.error} onDismiss={attachments.dismissError} />
            <form
              onSubmit={handleSubmit}
              className="flex flex-col border border-border-strong bg-surface shadow-2xl focus-within:border-accent focus-within:ring-2 focus-within:ring-[var(--ring)] transition-all p-1.5 rounded-md"
              {...attachments.dropHandlers}
            >
              {/* Inside the border, not floating above it: they belong to the
                  message being written, and a bar above the composer read as a
                  notification about them. */}
              <AttachmentChips images={attachedImages} onRemove={attachments.remove} />

              <div className="flex items-end gap-1.5">
              <div className="pl-4 pr-2 py-4 text-text-faint shrink-0">
                <Terminal className={cn("h-4 w-4", isLoading ? "animate-pulse text-green-500" : "")} />
              </div>
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => {
                  setInput(e.target.value);
                  // Auto-resize
                  const ta = e.target;
                  ta.style.height = 'auto';
                  ta.style.height = Math.min(ta.scrollHeight, 180) + 'px';
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    // Typing ahead is allowed; sending a second turn on top of
                    // a running one is not — `handleSubmit` returns early, and
                    // the draft is kept rather than swallowed.
                    if (input.trim() || attachedImages.length > 0) {
                      handleSubmit(e as unknown as React.FormEvent);
                    }
                  }
                  if (e.key === 'Escape' && editingIndex !== null) {
                    setEditingIndex(null);
                    setInput('');
                  }
                }}
                {...attachments.pasteHandlers}
                placeholder={isLoading ? "Agent is working — type your next message…" : "Enter command…"}
                title="Shift+Enter for a new line"
                // The <form> above owns the focus ring for this control.
                data-no-focus-ring
                className="flex-1 bg-transparent py-3 pr-2 text-[15px] focus:outline-none font-mono text-zinc-100 placeholder:text-zinc-500 resize-none min-h-[44px] max-h-[200px] leading-relaxed"
                rows={1}
                autoFocus
              />
              <AttachButton
                onFiles={attachments.accept}
                count={attachedImages.length}
                disabledReason={canAttach ? '' : "This agent's model cannot read images"}
                className="p-2.5 self-end mb-1 relative top-[1px]"
              />
              {/* The one button, in whichever job the moment calls for.

                  Two buttons would mean one of them is always dead, and the
                  live one moves — so Stop lands where the eye already is,
                  which is where Execute was a second ago. */}
              {isLoading ? (
                <button
                  type="button"
                  onClick={handleStop}
                  title="Stop the agent"
                  className="px-4 py-2 bg-surface-2 text-text font-semibold text-[11px] uppercase tracking-wider border border-border-strong hover:bg-row-hover hover:border-accent transition-all self-end mb-1 shrink-0 flex items-center gap-1.5 justify-center rounded-md"
                >
                  <Square className="h-3 w-3 fill-current" />
                  <span className="hidden md:inline">Stop</span>
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!input.trim() && attachedImages.length === 0}
                  className="px-4 py-2 bg-accent text-accent-fg font-semibold text-[11px] uppercase tracking-wider border border-transparent hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-all self-end mb-1 shrink-0 flex items-center justify-center rounded-md"
                >
                  <span className="hidden md:inline">{editingIndex !== null ? 'Resend' : 'Execute'}</span>
                  <Send className="h-4 w-4 md:hidden" />
                </button>
              )}
              </div>
            </form>
            <div className="text-center mt-2">
              <p className="text-xs text-text-faint uppercase tracking-widest font-mono">
                Synapses that connect agents
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
