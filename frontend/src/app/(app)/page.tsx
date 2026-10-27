'use client';

import { useState, useRef, useEffect, useCallback, useId, useMemo } from 'react';
import { Bot, User, Plus, Server, ChevronDown, ChevronRight, GitBranch, AlertCircle, History, RefreshCw, Clock, Trash2, X, Pin, Paperclip, Cpu, Wrench, Network, CalendarClock, Sparkles, Copy, Check, Brain, Pencil, RotateCcw, Search, Square, Users, Globe, MessageSquare } from 'lucide-react';
import * as RPopover from '@radix-ui/react-popover';
import { Command } from 'cmdk';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { putAgentDraft } from '@/lib/agentDraft';
import { CollectDataForm } from '@/components/CollectDataForm';
import { HumanStepAnswer } from '@/components/HumanStepAnswer';
import { ActiveRunsBanner } from '@/components/ActiveRunsBanner';
import { AttachButton, AttachmentChips, AttachmentError, useImageAttachments } from '@/components/app/ImageAttachments';
import { ImageThumbnails } from '@/components/app/ImageThumbnails';
import { BuilderConversation } from '@/components/app/BuilderPanel';
import { useBuilderChat } from '@/components/app/BuilderProvider';
import { Button, IconButton, Mark, Markdown, PageHeaderSlot, Spinner, useOwnScroll, usePersisted } from '@/components/ui';
import { useIsPhone } from '@/components/app/PhoneBar';
import {
  MAX_TABS, NewTabMenu, SideRail, TabStrip, TabSwitcher, chatKey, draftHome, openTab, readAssistantDraft, restoreChatTabs,
  restorePins, SUGGESTED_LABEL, useChatTabs, useTabShortcuts, withoutAssistantDraft,
  type ChatTab, type PickableAgent, type Pin as TabPin, type RailPanel, type SavedChatTabs, type StripTab, type TabStatus,
  type TabsState,
} from '@/components/tabs';
import {
  AgentAvatar, AgentIntro, AgentTurn, Composer, composerPillClass, HelperLanes, HelperNote, PillAvatar, RunChip, UserBubble,
  agentFromHelper, foldHelpers, isHelperEvent, laneFromRecord, toolsUsedBesideLanes, type HelperLaneData,
} from '@/components/assistant';
import { StepIcon } from '@/components/run';
import {
  LEAD_TOOLS, TeamView, answeredHere, teamActivity, emptyTeam, foldTeam, hasTeam, isTeamEvent, personNote, teamFromTurn,
  teamSummaryLine, workingMembers, type MemberAnswer, type MemberLaneData, type TeamState,
} from '@/components/team';

import { cn } from '@/lib/utils';
import { OSS_BROWSER_HOST } from '@/lib/browserHost';
import {
  BrowserActionLine, BrowserPane, BrowserTurnCard, PANE_SHARE, PaneHandle, actionOf, browserTurnOf, isBrowserEvent,
  parseShare,
  type BrowserAction, type BrowserEvent, type FrameMemory,
} from '@/components/browser';
import { readWithStallTimeout } from '@/lib/sse';
import { formatRelativeTime } from '@/lib/relativeTime';
import { refusedAnswerMessage } from '@/lib/answers';
import { useModelSetup } from '@/components/onboarding/ModelSetupProvider';
import { KnowledgeCitation, Message, OrchMsgType, SystemStatus } from '@/types';
import { KnowledgeSources, linkLabels } from '@/components/knowledge/KnowledgeSources';

/** What a chat whose agent was deleted calls it; the id stays on hover where the row shows it (F1129). */
const GONE_AGENT = 'Deleted agent';

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

/**
 * A workflow's answer, said once (U67). Its last step's output *is* the answer,
 * so the step's card and the answer bubble below it carried the same text. The
 * step card keeps its place — it says which step answered — and points down.
 */
function answerOnce(prev: Message[], answer: string): Message[] {
  const text = (answer || '').trim();
  if (!text) return prev;
  const at = prev.findLastIndex(m => m.msgType === 'agent_step_result');
  if (at === -1 || (prev[at].content || '').trim() !== text) return prev;
  if (prev.slice(at + 1).some(m => m.role === 'user')) return prev;
  const next = [...prev];
  next[at] = { ...next[at], data: { ...(next[at].data ?? {}), answerBelow: true } };
  return next;
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
    <div className="w-0 min-w-full overflow-hidden rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 bg-surface px-3 py-1.5 text-xs text-text-muted transition-colors hover:bg-surface-2 hover:text-text"
      >
        {open ? <ChevronDown className="h-3 w-3" aria-hidden /> : <ChevronRight className="h-3 w-3" aria-hidden />}
        <span>
          💭 Reasoning {stepName ? `· ${stepName}` : ''} ({parsedThoughts.length} turn{parsedThoughts.length > 1 ? 's' : ''})
        </span>
      </button>
      {open && (
        <div className="divide-y divide-border border-t border-border bg-surface-2">
          {parsedThoughts.map((entry, i) => (
            <div key={i} className="px-3 py-2 text-[13px] leading-6">
              {parsedThoughts.length > 1 && (
                <span className="mr-2 text-2xs text-text-faint">Turn {i + 1}</span>
              )}
              {entry.type === 'tool_call' ? (
                <span className="text-text-faint">→ Called: <span className="font-code" translate="no">{entry.toolName}</span></span>
              ) : (
                <span className="whitespace-pre-wrap break-words text-text-muted">{entry.text}</span>
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
    <div className="w-0 min-w-full overflow-hidden rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 bg-surface px-3 py-1.5 text-xs text-text-muted transition-colors hover:bg-surface-2 hover:text-text"
      >
        {open ? <ChevronDown className="h-3 w-3" aria-hidden /> : <ChevronRight className="h-3 w-3" aria-hidden />}
        <Brain className="h-3 w-3 text-machine" aria-hidden />
        <span>
          Thinking {stepName ? `· ${stepName}` : ''} ({blocks.length} turn{blocks.length > 1 ? 's' : ''})
        </span>
      </button>
      {open && (
        <div className="divide-y divide-border border-t border-border bg-surface-2">
          {blocks.map((block, i) => (
            <div key={i} className="px-3 py-2 text-[13px] italic leading-6">
              {blocks.length > 1 && (
                <span className="mr-2 text-2xs not-italic text-text-faint">Turn {i + 1}</span>
              )}
              <span className="whitespace-pre-wrap break-words text-text-muted">{block}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Orchestration Step Divider ──────────────────────────────────────────────
// The step's own icon on its role's ground (cyan a model, grey logic, amber a
// person), as the canvas and the run page draw it.
function StepDivider({ stepName, stepType }: { stepName: string; stepType?: string }) {
  return (
    <div className="my-1 flex items-center gap-3">
      <div className="h-px flex-1 bg-border" />
      <div className="flex shrink-0 items-center gap-2 text-xs text-text-faint">
        {stepType ? <StepIcon type={stepType} className="size-5" /> : <span aria-hidden>▶</span>}
        <span className="text-text-muted">{stepName}</span>
        {stepType && <span>· {stepType}</span>}
      </div>
      <div className="h-px flex-1 bg-border" />
    </div>
  );
}

// ─── Orchestration Info Banner ───────────────────────────────────────────────
function OrchBanner({ content, variant }: { content: string; variant: 'start' | 'complete' | 'error' }) {
  const styles: Record<string, string> = {
    start: 'border-border bg-machine-subtle text-accent-ink',
    complete: 'border-border bg-surface text-text-muted',
    error: 'border-danger/40 bg-danger-subtle text-danger',
  };
  return (
    <div className="my-2 flex justify-center">
      <div className={cn('rounded-full border px-4 py-1 text-xs', styles[variant])}>
        {content}
      </div>
    </div>
  );
}

/** Copy an answer: the button under it, named for a screen reader as well as the pointer. */
function CopyAnswer({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
      aria-label="Copy response"
      title="Copy response"
      className="rounded-md p-1 text-text-faint opacity-0 transition-opacity hover:bg-surface-2 hover:text-text focus-visible:opacity-100 group-hover:opacity-100"
    >
      {copied ? <Check className="h-3.5 w-3.5" aria-hidden /> : <Copy className="h-3.5 w-3.5" aria-hidden />}
    </button>
  );
}

// ─── Sub-agent Result Bubble ─────────────────────────────────────────────────
function AgentStepResult({ msg, onCollectDataSubmit }: {
  msg: Message;
  onCollectDataSubmit: (values: Record<string, unknown>) => void;
}) {
  // Fallback chain: content → data.content → data.result → empty
  const displayContent = msg.content || (msg.data as any)?.content || (msg.data as any)?.result || '';
  const anchor = `src-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  return (
    <AgentTurn
      avatar={<AgentAvatar className="bg-machine-subtle text-accent-ink"><Bot className="h-3.5 w-3.5" aria-hidden /></AgentAvatar>}
      aside={(msg.data as { answerBelow?: boolean } | undefined)?.answerBelow ? undefined : <CopyAnswer text={displayContent} />}
      className="group"
    >
      {msg.stepName && (
        <div className="text-xs font-medium text-text-faint">{msg.stepName}</div>
      )}
      {msg.reasoning && msg.reasoning.length > 0 && (
        <ReasoningCollapsible reasoning={msg.reasoning} stepName={msg.stepName} />
      )}
      {(msg.data as { answerBelow?: boolean } | undefined)?.answerBelow ? (
        // Its output is the workflow's answer, drawn once, below (U67).
        <p className="text-[13px] text-text-faint">Its output is the answer below.</p>
      ) : (
        <div className="min-w-0 rounded-xl bg-surface-2 px-4 py-3">
          <Markdown className="text-[15px] leading-relaxed">{linkLabels(displayContent, msg.citations, anchor)}</Markdown>
        </div>
      )}
      <KnowledgeSources citations={msg.citations} unresolved={msg.unresolvedCitations} anchor={anchor} />
      {/* Thoughts */}
      {msg.thoughts && msg.thoughts.length > 0 && (
        <ThoughtCollapsible thoughts={msg.thoughts} stepName={msg.stepName} />
      )}
      {/* Intent-based UI */}
      {msg.intent === 'collect_data' && msg.data && (
        <CollectDataForm data={msg.data} onSubmit={onCollectDataSubmit} />
      )}
    </AgentTurn>
  );
}

// ─── Assistant Message ───────────────────────────────────────────────────────
// The answer as text beside the agent, with no box around it; what ran on the
// way is a quiet chip under it, and its Sources are chips too (M2c).
function AssistantBubble({ msg, agentName }: { msg: Message; agentName: string }) {
  const anchor = `src-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const saved = (msg.toolCalls ?? (Array.isArray(msg.data?.calls) ? msg.data.calls : [])) as SavedCall[];
  // A helper's send is its lane, drawn above (F572).
  const besideHelpers = toolsUsedBesideLanes(saved, msg.helpers?.length ?? 0);
  // A Team agent's assign, message, wait and forward: its task list and lanes show what each did (TEAM1b).
  const calls = hasTeam(msg.team)
    ? besideHelpers.filter(c => !(LEAD_TOOLS as readonly string[]).includes(c.tool)) : besideHelpers;
  const teamLine = teamSummaryLine(msg.team);
  return (
    <div data-answer className="flex min-w-0 max-w-full flex-col gap-2.5">
      {msg.intent && (
        // One line, whatever the name: at three lines this tag covered the
        // answer it labels (F217).
        <div data-answer-name className="max-w-full truncate text-xs font-medium text-text-faint" title={agentName}>
          {agentName}
        </div>
      )}
      {msg.reasoning && msg.reasoning.length > 0 && (
        <ReasoningCollapsible reasoning={msg.reasoning} />
      )}
      <Markdown className="text-[15px] leading-relaxed">{linkLabels(msg.content, msg.citations, anchor)}</Markdown>
      {msg.images && msg.images.length > 0 && <ImageThumbnails images={msg.images} />}
      {calls.length > 0 && <ToolsUsed calls={calls} />}
      <KnowledgeSources citations={msg.citations} unresolved={msg.unresolvedCitations} anchor={anchor} />
      {msg.thoughts && msg.thoughts.length > 0 && (
        <ThoughtCollapsible thoughts={msg.thoughts} />
      )}
      {msg.refused && msg.refused.length > 0 && (
        // What this turn did not run (U68): the answer above may say it was done.
        <ul className="space-y-0.5 text-xs text-warning" aria-label="Calls not run">
          {msg.refused.map((r, i) => (
            <li key={i} className="flex gap-1.5">
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>Not run: <span className="font-code" translate="no">{r.tool}</span>{r.reason ? ` — ${r.reason}` : ''}</span>
            </li>
          ))}
        </ul>
      )}
      {msg.notes && msg.notes.length > 0 && (
        // What the turn warned of beside its answer (F584): the answer above may say the card is closed.
        <ul className="space-y-0.5 text-xs text-warning" aria-label="Notes on this answer">
          {msg.notes.map((note, i) => (
            <li key={i} className="flex gap-1.5">
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>{note}</span>
            </li>
          ))}
        </ul>
      )}
      {teamLine && (
        // How a Team agent's turn went (Q1b): it answered alone, or what its members took.
        <p className="inline-flex items-center gap-1.5 text-xs text-text-faint" aria-label="This turn's team">
          <Users className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {teamLine}
        </p>
      )}
    </div>
  );
}

/**
 * The tools an answer used, as one quiet chip that opens to each call: its
 * arguments and the start of what it returned. A reopened answer has them from
 * the calls its turn saved (F242, U60); a live one from the events it streamed.
 */
function ToolsUsed({ calls }: { calls: SavedCall[] }) {
  const refused = calls.filter(c => c.refused).length;
  return (
    <RunChip
      icon={<Wrench className="h-3.5 w-3.5" />}
      label={<>Tools used · {calls.length}{refused > 0 && <span className="text-warning"> · {refused} not run</span>}</>}
      detail={(
        <ul className="space-y-2 border-l border-border pl-3 text-xs">
          {calls.map((call, i) => {
            const args = call.args && Object.keys(call.args).length > 0 ? JSON.stringify(call.args) : '';
            return (
              <li key={i} className="min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className="font-code text-text" translate="no">{call.tool}</span>
                  {call.refused && <span className="text-warning">not run{call.reason ? ` — ${call.reason}` : ''}</span>}
                </div>
                {call.browser ? (
                  <BrowserActionLine
                    action={call.browser}
                    className="mt-1 text-text-muted"
                    shot={call.browser.shot_ref ? OSS_BROWSER_HOST.shotUrl(call.browser.session_id, call.browser.shot_ref) : null}
                  />
                ) : args && <div className="truncate font-code text-2xs text-text-faint" title={args}>{args}</div>}
                {call.result && (
                  <div className="line-clamp-2 whitespace-pre-wrap break-words text-2xs text-text-faint">
                    {call.result.slice(0, 300)}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    />
  );
}

/** Images a reopened question carried: named, since the image itself is not kept (F245, U60). */
function ImageNoteChip({ notes }: { notes: { type: string }[] }) {
  const kinds = [...new Set(notes.map(n => (n.type || 'image').replace(/^image\//, '').toUpperCase()))].join(', ');
  return (
    <div className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-text-faint">
      <Paperclip className="h-3 w-3" aria-hidden />
      {notes.length} image{notes.length === 1 ? '' : 's'} ({kinds}) — not kept after the turn
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

/** One tool call as a saved turn keeps it (F242); `refused` marks a call that was not run (U68). */
type SavedCall = { tool: string; args: Record<string, unknown> | null; result: string; refused?: boolean; reason?: string;
                   /** What a browser tool did in the page, while the turn is on screen (BRW2, F1074). */
                   browser?: BrowserAction };

type SessionTurn = {
  user: string;
  assistant: string;
  tools: string[];
  timestamp: string;
  /** What the turn's tools did, as the conversation keeps it (F242) — drawn under a reopened answer (U60). */
  calls?: SavedCall[];
  /** The images the question carried — their types; the image itself is not kept (F245). */
  images?: { type: string }[];
  /** A question this turn asked and nobody answered: the card, and why it was not run (F247). */
  approval?: ParkedApproval & { outcome: 'expired'; why: string };
  /** What the turn's model reasoned (`llm_reasoning`) and thought (`llm_thought`), in order —
   *  the Thinking and Reasoning panels a reopened answer draws again (U60). Absent on older turns. */
  reasoning?: string[];
  thoughts?: string[];
  /** The answer's Sources, as the turn saved them (Knowledge, B1b). Absent on most turns. */
  citations?: KnowledgeCitation[];
  unresolved_citations?: string[];
  /** The helpers the turn sent, each one's record (SUB1a): their lanes, reopened. */
  helpers?: Record<string, unknown>[];
  /** A Team agent's members, each one's record, and its task list and summary (TEAM1b). */
  members?: Record<string, unknown>[];
  team?: Record<string, unknown>;
  /** Set when nobody typed the question: a helper's result posted back (SUB1a). */
  automated?: Automated;
  /** What the turn warned of beside its answer (F584): a board card it left claimed. */
  notes?: string[];
};

/** The loop's own line before every model call (`react_engine.py`): no news once the agent has had a thought. */
const GENERIC_THINKING = 'Analyzing your request...';

/**
 * One line of a thought for the live activity line (U57): its first line with
 * the markdown taken off — Gemini's summaries lead with a bold title. With
 * `proseOnly`, text that is a tool call (JSON, a code fence) is not a thought.
 */
function thoughtLine(text: string, proseOnly = false): string | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (proseOnly && /^[[{`<]/.test(trimmed)) return null;
  const first = trimmed.split('\n').map(l => l.trim()).find(Boolean) ?? '';
  const plain = first.replace(/^#+\s*/, '').replace(/^[-*]\s+/, '').replace(/\*\*|__|`/g, '').trim();
  if (!plain) return null;
  return plain.length > 140 ? `${plain.slice(0, 139)}…` : plain;
}

/** Why a card was not run when a new message moved past it — the server's words (`chat_approval.MOVED_ON`). */
const MOVED_ON = 'you sent a new message instead';

/** One of a team's questions (TEAM1c): each member asks on its own, answered by its own id. */
type TeamQuestion = {
  question_id: string;
  approval_id: string;
  member_id: string | null;
  member_name: string | null;
  tool_name: string;
  tool_args: Record<string, unknown>;
  category: string;
  prompt?: string;
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
  /** Every question it asks: one, or a team's members' (TEAM1c). The fields above are the first's. */
  questions?: TeamQuestion[];
};

/** A turn still being written. A turn is saved when it ends, so it is not in `turns` yet. */
/** `automated`: a helper's follow-up nobody typed (SUB1a) — drawn as the helper's, never the person's. */
type RunningTurn = { turn_id: string; message: string; started_at: number; cursor: string; automated?: Automated };

/** A question nobody typed: a background helper's result, posted back (SUB1a). */
type Automated = {
  kind: string; helper_id?: string; name?: string; status?: string; session_id?: string | null;
  /** Several helpers' results, come back together as one follow-up: every one of them (SUB1a). */
  helper_ids?: string[]; names?: string[]; statuses?: string[];
};
/** Whether a follow-up's question holds this helper's result. */
const holdsHelper = (automated: Automated | undefined, helperId: string) =>
  !!automated && (automated.helper_ids ?? [automated.helper_id]).includes(helperId);

type StepState = {
  thoughts: string[];
  reasoning: string[];
  orchStepId: string | null;
  receivedAgentStep: boolean;
};

const newStepState = (): StepState => (
  { thoughts: [], reasoning: [], orchStepId: null, receivedAgentStep: false }
);

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
  msg, onDecide, disabled, movedPast,
}: {
  msg: Message;
  onDecide: (approve: boolean, reason: string) => void;
  disabled: boolean;
  /** A message was sent after it: the call was refused, not asked any more (F247). */
  movedPast: boolean;
}) {
  const [reason, setReason] = useState('');
  const [answered, setAnswered] = useState(false);
  const args = msg.data?.tool_args as Record<string, unknown> | undefined;
  // Why it was not run, as the server kept it; a card moved past on this page
  // has not been read back yet, and a new message is the only way to move on.
  const why = (msg.data?.why as string | undefined) ?? MOVED_ON;
  const expired = !answered && (movedPast || msg.data?.expired === true);

  return (
    <AgentTurn avatar={<AgentAvatar className="bg-person-subtle text-person-ink"><AlertCircle className="h-4 w-4" aria-hidden /></AgentAvatar>}>
      <div className="flex min-w-0 flex-col gap-3 rounded-xl border border-person/60 bg-surface px-4 py-3.5">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-person-ink">
          <span aria-hidden className="size-2 rounded-full bg-person" />
          Permission needed
        </span>
        <Markdown className="text-[15px] leading-relaxed">{msg.content}</Markdown>

        {args && Object.keys(args).length > 0 && (
          <pre className="overflow-x-auto rounded-lg border border-border bg-surface-2 p-3 font-code text-xs text-text">
            {JSON.stringify(args, null, 2)}
          </pre>
        )}

        {answered ? (
          <p className="text-[13px] text-text-faint">Answered.</p>
        ) : expired ? (
          <p className="text-[13px] text-text-faint">
            {why === MOVED_ON ? 'You moved on — this was not run.' : `Not run — ${why}.`}
          </p>
        ) : (
          <>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              aria-label="Reason for refusing"
              placeholder="Why not? (optional — the agent reads this and works around it)"
              disabled={disabled}
              className="w-full rounded-md border border-border-strong bg-surface px-3 py-2 text-sm text-text placeholder:text-text-faint disabled:opacity-50"
            />
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="person"
                size="sm"
                disabled={disabled}
                onClick={() => { setAnswered(true); onDecide(true, ''); }}
              >
                Allow once
              </Button>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                disabled={disabled}
                onClick={() => { setAnswered(true); onDecide(false, reason); }}
              >
                Refuse
              </Button>
            </div>
            <p className="text-xs text-text-faint">
              Allowing applies to this call and these arguments only.
            </p>
          </>
        )}
      </div>
    </AgentTurn>
  );
}

/**
 * The agent switcher: a search box and a list that scrolls (F271).
 *
 * It was the kit's `Menu` — no max height and `overflow: hidden`, so with 55
 * agents it was 1,770 px tall in a 1,000 px window and everything past the
 * fold could not be picked. A dropdown menu owns every keystroke for its own
 * type-ahead, so a filter box cannot live in it; this is the model picker's
 * shape (`Combobox`: a Popover and `cmdk`) with the chat header's own trigger.
 * Picking closes it.
 */
function AgentPicker({
  agents, activeId, activeName, onPick,
}: {
  agents: [string, { name: string; status: string } | string][];
  activeId: string | null | undefined;
  /** The agent this conversation talks to, shown on the pill. */
  activeName: string;
  onPick: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  // Scrolls itself wherever it opens (F700, `own-scroll.ts`).
  const ownScroll = useOwnScroll<HTMLDivElement>();
  return (
    <RPopover.Root open={open} onOpenChange={setOpen}>
      {/* The composer's pill (M2c): the agent this conversation talks to.
          Named "Agents: …" — the list it opens is the agents. */}
      <RPopover.Trigger
        type="button"
        aria-label={`Agents: ${activeName}`}
        className={cn(composerPillClass, 'data-[state=open]:bg-row-hover')}
      >
        <PillAvatar><Bot className="h-3 w-3" /></PillAvatar>
        <span className="min-w-0 truncate" title={activeName}>{activeName}</span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-text-faint" aria-hidden />
      </RPopover.Trigger>
      <RPopover.Portal>
        <RPopover.Content
          align="start"
          side="top"
          sideOffset={6}
          collisionPadding={8}
          aria-label="Agents"
          className="z-50 flex w-72 max-w-[calc(100vw-16px)] max-h-[min(28rem,var(--radix-popover-content-available-height))] flex-col overflow-hidden rounded-lg border border-border-strong bg-surface shadow-elevated"
        >
          <Command loop className="flex min-h-0 flex-col">
            <div className="flex items-center gap-2 border-b border-border px-3">
              <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
              <Command.Input
                autoFocus
                placeholder="Search agents…"
                aria-label="Search agents"
                data-no-focus-ring
                className="h-9 w-full bg-transparent text-sm text-text outline-none placeholder:text-text-faint"
              />
            </div>
            <Command.List ref={ownScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-1">
              <Command.Empty className="px-2 py-6 text-center text-sm text-text-faint">
                {agents.length === 0 ? 'No agents detected.' : 'No agent matches.'}
              </Command.Empty>
              {agents.map(([id, info]) => {
                // A legacy string entry is the status alone.
                const name = typeof info === 'string' ? id : info.name;
                const status = typeof info === 'string' ? info : info.status;
                const isActive = activeId === id;
                return (
                  <Command.Item
                    key={id}
                    value={`${name} ${id}`}
                    onSelect={() => { setOpen(false); onPick(id); }}
                    className="relative flex cursor-pointer select-none items-center gap-2.5 rounded-md px-2 py-1.5 text-sm text-text outline-none data-[selected=true]:bg-surface-2"
                  >
                    <div className={cn('h-1.5 w-1.5 shrink-0 rounded-full', status === 'online' ? 'bg-success' : 'bg-danger')} aria-hidden />
                    <span className="min-w-0 flex-1 truncate" title={name}>{name}</span>
                    {isActive && (
                      <>
                        <Check className="size-4 shrink-0 text-accent-ink" aria-hidden />
                        <span className="sr-only">(current)</span>
                      </>
                    )}
                  </Command.Item>
                );
              })}
            </Command.List>
          </Command>
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
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

/** What the empty chat says about the agent: from `/api/agents`, its knowledge named from the bases list. */
type AgentIntroData = { description: string | null; tools: string[]; knowledge: string[] };

/**
 * A new conversation opens on the agent it is with (M2c, Naveen's call; Prototype 2):
 * its name, model and state, what it is for and what it can use. Below it, quieter,
 * the examples banner (its first 12 days) and the ways to build more.
 */
function WelcomeScreen({ agentName, modelReady, modelProblem, isWorkflow = false, model, intro, onNavigate, showExamplesBanner, onDismissBanner, onConnectModel }: {
  agentName: string; modelReady?: boolean; modelProblem?: string | null; isWorkflow?: boolean;
  onConnectModel: () => void;
  model: React.ReactNode; intro: AgentIntroData | null;
  onNavigate: (path: string) => void; showExamplesBanner: boolean; onDismissBanner: () => void;
}) {
  // These used to point at /settings/* — the chat page telling you to open
  // Settings in order to use the product.
  const PILLS = [
    { label: 'Build an agent', path: '/agents', icon: Bot },
    { label: 'Add a tool', path: '/tools', icon: Wrench },
    { label: 'Build workflows', path: '/workflows', icon: Network },
    { label: 'Connect apps', path: '/apps', icon: Server },
    { label: 'Schedule a task', path: '/schedules', icon: CalendarClock },
  ];

  // "Ready" is said only once the backend has checked that the model can
  // answer; it used to be said unconditionally, above a first message that then
  // failed with an HTTP library's text (F182).
  const status = modelReady === undefined ? null : (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden className={cn('size-2 rounded-full', modelReady ? 'bg-success' : 'bg-warning')} />
      {modelReady ? 'Ready' : isWorkflow ? 'A step is not ready' : 'Model not ready'}
    </span>
  );

  return (
    <div className="flex min-h-full select-none flex-col items-center justify-center gap-8 px-4 pb-44 pt-6 md:pt-10">
      <div className="flex w-full flex-col items-center gap-3">
        <AgentIntro
          name={agentName}
          model={model}
          status={status}
          description={intro?.description}
          tools={intro?.tools}
          knowledge={intro?.knowledge}
        />
        {modelReady === false && modelProblem && (
          <p className="max-w-md text-center text-[13px] leading-relaxed text-text-muted">
            <WithCode text={modelProblem} />{' '}
            {/* The agent's own model opens the first-run step (ONB2, Naveen 2026-10-20); a workflow's
                steps each name theirs, so it keeps Settings → Models. */}
            {isWorkflow ? (
              <Link href={MODELS_SETTINGS} className="whitespace-nowrap font-medium text-accent-ink underline-offset-2 hover:underline">
                Open Settings → Models
              </Link>
            ) : (
              <button type="button" onClick={onConnectModel} data-connect-model
                className="whitespace-nowrap rounded font-medium text-accent-ink underline-offset-2 hover:underline">
                Connect a model
              </button>
            )}
          </p>
        )}
      </div>

      {/* ── Welcome Examples Banner (12-day, session-dismissible) ── */}
      {showExamplesBanner && (
        <div className="w-full max-w-2xl rounded-xl border border-border bg-accent-subtle px-4 py-3">
          <div className="flex flex-col gap-2.5">
            {/* Top row: icon + headline + close */}
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 flex-1 items-center gap-2.5">
                <Sparkles className="h-4 w-4 shrink-0 text-accent-ink" aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-text">Get started in seconds</p>
                  <p className="break-words text-[13px] text-text-muted">Pre-built agents, workflows &amp; apps — ready to explore</p>
                </div>
              </div>
              <button
                id="examples-banner-close"
                type="button"
                onClick={onDismissBanner}
                className="mt-0.5 shrink-0 cursor-pointer rounded-md p-1 text-text-muted transition-colors hover:bg-surface hover:text-text"
                aria-label="Dismiss banner"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            {/* Bottom row: teaser pills + CTA */}
            <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
              <div className="flex flex-wrap items-center justify-center gap-1.5 sm:justify-start">
                {['Web research agent', 'Multi-step workflow', 'Apps'].map(label => (
                  <span key={label} className="rounded-full border border-border bg-surface px-2.5 py-0.5 text-xs text-text-muted">
                    {label}
                  </span>
                ))}
                <span className="text-xs text-accent-ink">+ more →</span>
              </div>
              <Button
                id="examples-banner-link"
                type="button"
                size="sm"
                onClick={() => onNavigate('/templates')}
                className="w-full shrink-0 sm:w-auto"
              >
                <Sparkles className="h-3.5 w-3.5" aria-hidden /> Browse Examples
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* The ways to build more, quieter than the agent above them. */}
      <nav aria-label="Build more" className="flex max-w-3xl flex-wrap justify-center gap-x-1 gap-y-1">
        {PILLS.map(({ label, path, icon: Icon }) => (
          <button
            key={label}
            type="button"
            onClick={() => onNavigate(path)}
            className="flex cursor-pointer items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[13px] text-text-muted transition-colors hover:bg-surface-2 hover:text-text"
          >
            <Icon className="h-3.5 w-3.5" aria-hidden />
            {label}
          </button>
        ))}
      </nav>
    </div>
  );
}

/**
 * Why this agent takes no images, or '' when it does. `/api/status` answers it,
 * since it already resolves each agent's model; a workflow agent's comes from
 * its steps, with the reason (F313). The button, a drop and a paste share it.
 */
function refusalOf(status: SystemStatus | null, agentId: string | null | undefined): string {
  const info = agentId ? status?.agents?.[agentId] : undefined;
  if (typeof info !== 'object' || info === null || info.can_see_images !== false) return '';
  return info.attach_problem || "This agent's model cannot read images";
}

/** What came off the composer, as the start of the line that says why. */
function tookOff(count: number): string {
  return count === 1 ? 'Took the image off this message.' : `Took the ${count} images off this message.`;
}

function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

function generateSessionId() {
  const c = (globalThis as any).crypto;
  return c?.randomUUID?.() ?? `sess_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

/** A tab's title: the conversation's first question, on one line. */
function tabTitle(text: string): string {
  const line = text.replace(/\s+/g, ' ').trim();
  return line.length > 60 ? `${line.slice(0, 59)}…` : line;
}

/**
 * One tab's conversation (TABS1, D319). The shell mounts only the tab in
 * front, keyed by the tab: a tab going behind unmounts, which stops *reading*
 * its turn and nothing else, and one coming forward mounts and reopens its
 * conversation from the server — a turn still being written is followed from
 * its start, as a reload does (F49). So a switch can neither drop nor repeat a
 * turn, an approval, a helper or a team: each is drawn from what the server
 * holds.
 */
function ChatConversation({ tab, onChange, onBusy, onOpenAgent, onOpenChat, onBrowser, onShowBrowser, onResumeHook }: {
  tab: ChatTab;
  /** Its title, its draft, or — before its first message — its agent changed. */
  onChange: (change: Partial<ChatTab>) => void;
  /** Whether a turn is on screen: the front tab's badge. */
  onBusy: (busy: boolean) => void;
  /** Another agent was picked once this conversation had started: that agent opens in a tab of its own. */
  onOpenAgent: (agentId: string) => void;
  /** A conversation opened from inside this one (F1063's "Open that chat"): in its tab, or a new one. */
  onOpenChat: (sessionId: string, agentId: string, title: string) => void;
  /** The agent opened a browser, or did something in it (BRW2): the pane beside the conversation is the shell's. */
  onBrowser: (event: BrowserEvent, agentName: string) => void;
  /** A browser turn's "Open the browser": the shell shows that session's pane. */
  onShowBrowser: (sessionId: string, agentName: string) => void;
  /** Gives the shell what to call when the person hands the browser back in the pane: the waiting turn goes on. */
  onResumeHook: (resume: () => void) => void;
}) {
  const [sessionId, setSessionId] = useState(tab.sessionId);

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState(tab.draft ?? '');
  const [isLoading, setIsLoading] = useState(false);
  const [agentName, setAgentName] = useState('Loading...');
  // For a stream that started on an earlier render: who is driving the browser it opens (BRW2).
  const agentNameRef = useRef(agentName);
  agentNameRef.current = agentName;
  // Whether the name came from the active agent. The workspace's own name, from
  // settings, is only the fallback when no agent is active: both requests set
  // the name, and when /api/status answered first the page said "Synapse" in
  // place of the agent that answers (F229).
  const agentNamedRef = useRef(false);
  const router = useRouter();
  const modelSetup = useModelSetup();
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [streamingActivity, setStreamingActivity] = useState<string | null>(null);
  const [isThinking, setIsThinking] = useState(false);
  // First line of the model's latest reasoning/progress message — shown next
  // to the 💭 indicator so "Thinking" is never a blank box.
  const [thinkingPreview, setThinkingPreview] = useState<string | null>(null);
  // The answer as the model writes it (U72), shown in the working row until
  // `response` puts the answer of record in its place.
  const [draftAnswer, setDraftAnswer] = useState('');
  // The tab's agent. The workspace's "active agent" is only where a new tab starts; a tab keeps its own (TABS1).
  // A tab with no agent yet ('') is the page's no-agent state.
  const [currentAgentId, setCurrentAgentId] = useState<string | null>(tab.agentId || null);
  // The agent as it is now, for work that outlives the render that started it (a background helper's follow-up).
  const currentAgentIdRef = useRef<string | null>(tab.agentId || null);
  useEffect(() => { currentAgentIdRef.current = currentAgentId; }, [currentAgentId]);
  // The conversation is being read back from the server, or that read failed: never the empty
  // state meanwhile, which read as a conversation with nothing in it (F873's class).
  const [opening, setOpening] = useState(true);
  const [openFailed, setOpenFailed] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Whether attaching anything to this agent does something. Answered by
  // `/api/status`, which already resolves each agent's model — the alternative
  // was a second copy in here of the list of providers that cannot see, and a
  // silent drop every time the two disagreed. A workflow agent's comes from its
  // steps, with the reason (F313). The button, a drop and a paste share it.
  const attachRefusal = refusalOf(systemStatus, currentAgentId);
  // The default agent is Synapse AI, the assistant (ASSIST1): a builder-typed agent talks through its own conversation.
  const currentInfo = currentAgentId ? systemStatus?.agents?.[currentAgentId] : undefined;
  const isAssistant = typeof currentInfo === 'object' && currentInfo !== null && currentInfo.type === 'builder';
  // Shared with the AI builder's composer — one validator, one chip strip, one
  // counter, one set of refusal messages.
  const attachments = useImageAttachments(attachRefusal);
  const attachedImages = attachments.images;

  // A workflow agent has no model of its own: each step runs on the one set on
  // it, and `/api/status` resolves the workspace default for it. The header
  // says "Workflow · N steps" instead (U64).
  const workflowId = (() => {
    const info = currentAgentId ? systemStatus?.agents?.[currentAgentId] : undefined;
    return typeof info === 'object' && info !== null && info.type === 'orchestrator'
      ? info.orchestration_id || null
      : null;
  })();
  const [workflowSteps, setWorkflowSteps] = useState<{ id: string; count: number } | null>(null);
  useEffect(() => {
    if (!workflowId) return;
    let live = true;
    fetch(`/api/orchestrations/${encodeURIComponent(workflowId)}`)
      .then(r => (r.ok ? r.json() : null))
      .then(o => {
        if (live && o && Array.isArray(o.steps)) setWorkflowSteps({ id: workflowId, count: o.steps.length });
      })
      .catch(() => {});
    return () => { live = false; };
  }, [workflowId]);
  const workflowStepCount = workflowSteps && workflowSteps.id === workflowId ? workflowSteps.count : null;

  // What the empty chat says about the agent (M2c): its description, tools and
  // knowledge, from the agents list; its knowledge named from the bases list,
  // read only when it has any.
  const [agentDetails, setAgentDetails] = useState<Record<string, { description?: string | null; tools?: string[]; knowledge?: string[]; taskBudget?: number | null }>>({});
  const [kbNames, setKbNames] = useState<Record<string, string> | null>(null);
  useEffect(() => {
    let live = true;
    fetch('/api/agents')
      .then(r => (r.ok ? r.json() : null))
      .then(list => {
        if (!live || !Array.isArray(list)) return;
        setAgentDetails(Object.fromEntries(list.map((a: { id: string; description?: string | null; tools?: string[]; knowledge?: string[];
                                                          team?: { mode?: string; task_budget?: number } | null }) =>
          [a.id, { description: a.description, tools: a.tools ?? [], knowledge: a.knowledge ?? [],
                   // A Team agent's task budget, for its task list's "3 of 6 tasks" (TEAM1b).
                   taskBudget: a.team?.mode === 'team' ? a.team.task_budget ?? null : null }])));
      })
      .catch(() => {});
    return () => { live = false; };
  }, []);
  const currentDetails = currentAgentId ? agentDetails[currentAgentId] : undefined;
  const needsKbNames = Boolean(currentDetails?.knowledge?.length) && kbNames === null;
  useEffect(() => {
    if (!needsKbNames) return;
    let live = true;
    fetch('/api/knowledge/bases')
      .then(r => (r.ok ? r.json() : []))
      .then(list => { if (live) setKbNames(Object.fromEntries((Array.isArray(list) ? list : []).map((b: { id: string; name?: string }) => [b.id, b.name || b.id]))); })
      .catch(() => { if (live) setKbNames({}); });
    return () => { live = false; };
  }, [needsKbNames]);
  const agentIntro: AgentIntroData | null = currentDetails ? {
    description: currentDetails.description ?? null,
    tools: currentDetails.tools ?? [],
    knowledge: (currentDetails.knowledge ?? []).map(id => kbNames?.[id] ?? id),
  } : null;

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
  /** The turn's newest thought, for the live line (U57): what the agent is doing, not "Analyzing…". */
  const lastThoughtRef = useRef<string | null>(null);
  /** The turn's calls that were not run (U68), shown under its answer. */
  const refusedRef = useRef<{ tool: string; reason: string }[]>([]);
  /** The turn's own tool calls as they streamed, for the chip under its answer (M2c) — what a reopened
   *  turn reads from its saved `calls`. The agent's own, not a delegate's or a workflow step's. */
  const liveCallsRef = useRef<NonNullable<Message['toolCalls']>>([]);
  // The helpers this turn sent (SUB1a): their lanes as they work, given to the answer when it comes.
  const liveHelpersRef = useRef<HelperLaneData[]>([]);
  const [liveHelpers, setLiveHelpers] = useState<HelperLaneData[]>([]);
  // A background helper's lane as its own conversation streams it since its turn ended, by id.
  const [backgroundLanes, setBackgroundLanes] = useState<Record<string, HelperLaneData>>({});
  const followedHelpersRef = useRef<Set<string>>(new Set());
  const [stoppingHelpers, setStoppingHelpers] = useState<Set<string>>(new Set());
  // A Team agent's turn (TEAM1b): its task list, notes and members' lanes as they work, given to the answer.
  const liveTeamRef = useRef<TeamState>(emptyTeam());
  const [liveTeam, setLiveTeam] = useState<TeamState>(emptyTeam());
  const [stoppingMembers, setStoppingMembers] = useState<Set<string>>(new Set());
  // Whom the composer writes to while a team works: the lead, or one member's id.
  const [sendTo, setSendTo] = useState<string>('lead');
  // What became of the person's last words to the team, under the composer.
  const [teamSendNote, setTeamSendNote] = useState<string | null>(null);
  // The turn on screen is a helper's follow-up: there is no question of the person's to retry.
  const automatedTurnRef = useRef(false);
  /** The answer's Sources (`knowledge_citations`, B1b), drawn under the answer that follows;
   *  a workflow step's under that step's result. */
  const sourcesRef = useRef<{ citations: KnowledgeCitation[]; unresolvedCitations: string[] } | null>(null);
  /** What the turn warns of beside its answer (`step_warning`, F584), drawn under the answer that follows. */
  const notesRef = useRef<string[]>([]);
  const stepSourcesRef = useRef<Record<string, { citations: KnowledgeCitation[]; unresolvedCitations: string[] }>>({});
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

  // How the last turn ended, when it did not end with an answer. Drives the
  // retry bar: a turn that broke is the one case where the person is left with
  // nothing to do and no way to try again, which until now meant retyping.
  // `model`: the model failed rather than the app (F182), so the bar points at
  // Settings → Models as well as offering the turn again.
  const [turnOutcome, setTurnOutcome] = useState<null | { kind: 'failed' | 'cancelled'; message?: string; model?: boolean }>(null);
  // A failed turn's one offer (F1018): the same question again, once, on a stronger model the server picked.
  const [strongerOffer, setStrongerOffer] = useState<null | { label: string; retry_of: string }>(null);
  // What to send if they do. Held in a ref rather than state because the retry
  // reads it from inside a handler, not during a render.
  const lastSentRef = useRef<{ content: string; images?: string[] } | null>(null);
  // Whether this turn actually produced an answer. A stream can end without
  // one — a proxy timing out mid-turn, a worker recycled — and that used to
  // look exactly like a finished conversation: the transcript simply stopped.
  const turnAnsweredRef = useRef(false);
  // Which conversation is on screen, as a count. New Chat, picking one from
  // History and switching agent each move it; a reopen that finds it moved
  // once its history arrives draws nothing. The reopen used to apply whenever
  // it answered, so New Chat pressed before then was undone and the next
  // message went into the old conversation (F188).
  const conversationEpochRef = useRef(0);
  // How many turns this page has started or followed, as a count. A read of the
  // conversation that went out before one started draws nothing: the page already
  // shows that turn live, and the read would draw it again and follow it a second
  // time, its answer twice (F552: a message sent before the page-open read answered).
  const turnsStartedRef = useRef(0);
  // Index of the user message being rewritten, or null. Editing one drops it
  // and everything after it — a conversation cannot fork.
  const [editingIndex, setEditingIndex] = useState<number | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // What the shell is told, through refs: a parent's new callback each render is not a change here.
  const onChangeRef = useRef(onChange);
  const onBusyRef = useRef(onBusy);
  const onOpenChatRef = useRef(onOpenChat);
  useEffect(() => { onChangeRef.current = onChange; onBusyRef.current = onBusy; onOpenChatRef.current = onOpenChat; });
  // The tab's title is the conversation's first question, as History's row is.
  const firstQuestion = messages.find(m => m.role === 'user' && !m.data?.automated)?.content ?? '';
  useEffect(() => {
    if (firstQuestion) onChangeRef.current({ title: tabTitle(firstQuestion) });
  }, [firstQuestion]);
  // A Synapse AI tab's chat says for itself whether it is working (`AssistantTab`).
  useEffect(() => { if (!isAssistant) onBusyRef.current(isLoading); }, [isLoading, isAssistant]);
  // The composer's words go with the tab when it goes behind, and come back with it.
  const inputRef = useRef(input);
  useEffect(() => { inputRef.current = input; }, [input]);
  // Once it has been on screen, a fresh tab's conversation is the server's to read back.
  useEffect(() => () => { onChangeRef.current({ draft: inputRef.current, fresh: false }); }, []);
  // A link's words handed to this tab (F1095: no Synapse AI to open them in) wait in the composer, unsent, said to be
  // a suggestion until the person makes them their own — as a Synapse AI tab's do.
  useEffect(() => {
    if (!tab.suggested || !tab.draft) return;
    setInput(tab.draft);
    requestAnimationFrame(() => {
      const field = textareaRef.current;
      if (field) { field.focus(); field.setSelectionRange(field.value.length, field.value.length); }
    });
  }, [tab.suggested, tab.draft]);
  const showSuggested = Boolean(tab.suggested && tab.draft) && input === tab.draft;
  const type = useCallback((text: string) => {
    setInput(text);
    if (tab.suggested && text !== tab.draft) onChangeRef.current({ suggested: false });
  }, [tab.suggested, tab.draft]);

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



  /**
   * Put a stored conversation back on the screen — all of what is open in it.
   *
   * Its transcript, a tool call it stopped to ask about (as the card, answerable
   * here), and a turn still being written: a turn is saved when it ends, so one
   * running now is not in the transcript yet — its question is shown and the
   * turn is read from its start (F49). Used when the page opens on the last
   * conversation and when one is picked from History.
   */
  const reopenConversation = async (
    sid: string, agentId: string | null, epoch = conversationEpochRef.current, since = turnsStartedRef.current,
  ) => {
    const running = await loadConversation(sid, agentId, epoch, since);
    if (running && epoch === conversationEpochRef.current && since === turnsStartedRef.current) {
      await watchTurn(sid, running.cursor, running.message, !!running.automated);
    }
  };

  /** Draw the conversation as stored; the turn still being written, if any, is returned to be followed. */
  const loadConversation = async (
    sid: string, agentId: string | null, epoch = conversationEpochRef.current, since = turnsStartedRef.current,
  ): Promise<RunningTurn | null> => {
    try {
      const url = `/api/sessions/${encodeURIComponent(sid)}/history${agentId ? `?agent_id=${encodeURIComponent(agentId)}` : ''}`;
      const res = await fetch(url);
      if (!res.ok) {
        if (epoch === conversationEpochRef.current) setOpenFailed(true);
        return null;
      }
      const data = await res.json();
      // Another conversation was put on screen while this one loaded (F188),
      // or a turn started here meanwhile, which the page shows live (F552).
      if (epoch !== conversationEpochRef.current || since !== turnsStartedRef.current) return null;
      setOpenFailed(false);
      const turns: SessionTurn[] = data.turns || [];
      const parked: ParkedApproval | null = data.pending_approval ?? null;
      const running: RunningTurn | null = data.running ?? null;
      if (turns.length === 0 && !parked && !running) return null;

      const restored: Message[] = [];
      for (const t of turns) {
        // A reopened turn keeps what it can show (U60): its thinking and
        // reasoning, the tools the answer used, and that images were attached —
        // they are not kept, so a chip says so rather than the turn reading as
        // text alone.
        restored.push({
          role: 'user', content: t.user,
          ...(t.images && t.images.length > 0 ? { data: { imageNotes: t.images } } : {}),
          // A question nobody typed — a helper's result (SUB1a) — drawn as the helper's.
          ...(t.automated ? { data: { automated: t.automated } } : {}),
        });
        if (t.approval) {
          // A question nobody answered, drawn where it was asked and not
          // answerable (F247) — it used to come back last, live.
          restored.push({
            role: 'assistant', content: t.approval.prompt, msgType: 'human_input_required',
            data: {
              kind: 'tool_approval', tool_name: t.approval.tool_name, tool_args: t.approval.tool_args,
              category: t.approval.category, approval_id: t.approval.approval_id,
              expired: true, why: t.approval.why,
            },
          });
          continue;
        }
        const notRun = (t.calls ?? []).filter(c => c.refused).map(c => ({ tool: c.tool, reason: c.reason ?? '' }));
        restored.push({
          role: 'assistant', content: t.assistant,
          ...(t.calls && t.calls.length > 0 ? { data: { calls: t.calls } } : {}),
          // Its panels, as live (U60): the last thought goes when it only
          // repeats the answer, which it usually does.
          ...(t.reasoning && t.reasoning.length > 0 ? { reasoning: t.reasoning } : {}),
          ...(t.thoughts && t.thoughts.length > 0 ? { thoughts: withoutEcho(t.thoughts, t.assistant) } : {}),
          // What the turn did not run, as live (U68).
          ...(notRun.length > 0 ? { refused: notRun } : {}),
          // Its Sources, as live (B1b).
          ...(t.citations?.length || t.unresolved_citations?.length
            ? { citations: t.citations ?? [], unresolvedCitations: t.unresolved_citations ?? [] } : {}),
          // Its helpers' lanes, as live (SUB1a).
          ...(t.helpers?.length ? { helpers: t.helpers.map(laneFromRecord) } : {}),
          // What it warned of, as live (F584).
          ...(t.notes?.length ? { notes: t.notes } : {}),
          // Its team, each member as one line that opens (TEAM1b).
          ...(teamFromTurn(t) ? { team: teamFromTurn(t) as TeamState } : {}),
        });
      }
      if (parked) {
        restored.push({ role: 'user', content: parked.message });
        restored.push({
          role: 'assistant', content: parked.prompt, msgType: 'human_input_required',
          data: {
            kind: 'tool_approval', tool_name: parked.tool_name, tool_args: parked.tool_args,
            category: parked.category, approval_id: parked.approval_id,
            // A team's: whose question the first is, and every one, each answered by its own id (TEAM1c).
            ...(parked.questions?.[0]?.member_id ? {
              question_id: parked.questions[0].question_id, member_id: parked.questions[0].member_id,
              delegate_agent_name: parked.questions[0].member_name, questions: parked.questions,
            } : {}),
          },
        });
      }
      if (running) restored.push({ role: 'user', content: running.message,
                                   ...(running.automated ? { data: { automated: running.automated } } : {}) });
      setMessages(restored);
      setTurnOutcome(null);
      setSessionId(sid);
      if (agentId) setCurrentAgentId(agentId);
      return running;
    } catch (e) {
      console.error('[Sessions] reopen error', e);
      if (epoch === conversationEpochRef.current) setOpenFailed(true);
      return null;
    }
  };

  // A background helper's lane goes on after its turn (SUB1a): followed once per helper.
  useEffect(() => {
    for (const msg of messages) {
      for (const lane of msg.helpers ?? []) {
        if (lane.mode !== 'background' || lane.status !== 'running' || !lane.sessionId) continue;
        if (followedHelpersRef.current.has(lane.id)) continue;
        followedHelpersRef.current.add(lane.id);
        void followHelper(lane, sessionId);
      }
    }
    // followHelper reads refs and the conversation it is given; `messages` is what changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages]);

  const refreshSystemStatus = () => {
    fetch('/api/status').then(r => r.json()).then(d => {
      setSystemStatus(d);
      // The name of this tab's agent. It used to be set from the workspace's active agent — and the
      // conversation's agent with it, so a read could move a conversation to another agent (TABS1).
      const id = currentAgentIdRef.current;
      const info = id ? d.agents?.[id] : undefined;
      if (id && info) {
        agentNamedRef.current = true;
        setAgentName(typeof info === 'string' ? id : info.name);
      } else if (id && d.agents) {
        // Not the workspace's agent name: the conversation would read as another agent's (F1129).
        agentNamedRef.current = true;
        setAgentName(GONE_AGENT);
      }
    }).catch(console.error);
  };

  // The first-run model step saved models or closed (ONB2): the line above the
  // composer says Ready, or what is still missing, without a reload.
  useEffect(() => {
    if (modelSetup.version) refreshSystemStatus();
  }, [modelSetup.version]);

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
        if (!agentNamedRef.current) setAgentName(d.agent_name || 'System Agent');
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
      .catch(() => { if (!agentNamedRef.current) setAgentName('Offline'); });

    // 2. The tab's agent, then its conversation
    initAgent();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Put this tab's conversation on screen: its agent made the workspace's active one — so the model
   * line and anything else that reads it say the same agent — then the conversation as the server
   * holds it, a running turn followed from its start (F49). A tab coming forward does exactly what a
   * reload does.
   */
  const initAgent = async () => {
    // Taken before anything is awaited: a switch of agent while this runs
    // moves it, and the old conversation is then not drawn back (F188).
    const epoch = conversationEpochRef.current;
    // And a message sent while this runs: its turn is drawn live, never again from here (F552).
    const since = turnsStartedRef.current;
    setOpening(true);
    setOpenFailed(false);
    try {
      // Written only when it differs: coming forward is a read, and most tabs share the workspace's agent.
      const status: SystemStatus | null = await fetch('/api/status').then(r => (r.ok ? r.json() : null));
      if (status && tab.agentId && status.active_agent_id !== tab.agentId) {
        await fetch('/api/agents/active', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ agent_id: tab.agentId }),
        });
      }
    } catch (e) {
      console.error('Failed to make the tab\'s agent the active one', e);
    }
    refreshSystemStatus();
    try {
      // A tab opened just now holds a new conversation: nothing on the server to read back.
      if (!tab.fresh && epoch === conversationEpochRef.current) {
        await reopenConversation(tab.sessionId, tab.agentId, epoch, since);
      }
    } finally {
      setOpening(false);
    }
  };

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

  const handleSwitchAgent = async (agentId: string) => {
    if (agentId === currentAgentId) return;
    // A conversation keeps its agent (TABS1): once it has started — or when it is Synapse AI's, the
    // builder's one conversation — another agent opens in a tab of its own, and so does Synapse AI.
    const picked = systemStatus?.agents?.[agentId];
    const pickedBuilder = typeof picked === 'object' && picked !== null && picked.type === 'builder';
    if (isAssistant || pickedBuilder || isLoading || messages.length > 0) {
      onOpenAgent(agentId);
      return;
    }
    // The new agent's conversation replaces whatever is still loading.
    conversationEpochRef.current += 1;
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
          agentNamedRef.current = true;
          setAgentName(name);
        } else if (agentId && statusData.agents) {
          agentNamedRef.current = true;
          setAgentName(GONE_AGENT);
        }
        setCurrentAgentId(agentId);

        // Images attached for the last agent that this one would not read come
        // off, and the line under the composer says why (F398): kept, they went
        // with the next message and were dropped by its provider.
        const refused = refusalOf(statusData, agentId);
        if (refused && attachedImages.length > 0) {
          attachments.clear();
          attachments.say(`${tookOff(attachedImages.length)} ${sentence(refused)}`);
        }

        // Generate new session for the new agent — isolates context. A turn the
        // old agent is still writing carries on in its own conversation.
        stopReading();
        const next = generateSessionId();
        setSessionId(next);
        // The tab is now this agent's conversation, and is saved as such.
        onChangeRef.current({ agentId, sessionId: next, title: '' });

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
    if (attachRefusal && imagesToSend.length > 0) {
      // Never sent to an agent that would not read them (F398); chat's route
      // refuses them too (F396). The text stays, to send again without them.
      attachments.clear();
      attachments.say(`${tookOff(imagesToSend.length)} ${sentence(attachRefusal)}`);
      return;
    }

    // Route to human-step resume if there's an unanswered human_input_required in history.
    // A workflow's human step only: a tool approval's card is answered by its
    // buttons, and a message typed under one refuses it as a new message (F247).
    // Sent to the run instead, the text was read as the approval's answer —
    // "ok" ran the call, and anything else was taken as the refusal's reason and
    // never answered (F298).
    const lastHumanIdx = messages.findLastIndex(
      // A browser turn is answered in the browser, by handing it back (BRW2): a message is a new message.
      m => m.msgType === 'human_input_required' && m.data?.kind !== 'tool_approval' && m.data?.kind !== 'browser_turn'
        && !browserTurnOf(m.data?.fields));
    if (lastHumanIdx !== -1) {
      const hasReplyAfter = messages.slice(lastHumanIdx + 1).some(m => m.role === 'user');
      if (!hasReplyAfter) {
        const pendingStep = messages[lastHumanIdx];
        if (pendingStep.data?.run_id) {
          // An answer carries words only. The images stay for the next message,
          // and the line says so rather than leaving them looking sent (F437).
          if (attachedImages.length > 0) {
            attachments.say(attachedImages.length === 1
              ? 'Your answer went without the image; it stays here for your next message.'
              : 'Your answer went without the images; they stay here for your next message.');
          }
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
  /** This turn's helpers' lanes, taken for the message they belong with (SUB1a). */
  const takeLiveHelpers = (): HelperLaneData[] => {
    const helpers = liveHelpersRef.current;
    liveHelpersRef.current = [];
    setLiveHelpers([]);
    return helpers;
  };

  /** Lanes with no answer to sit with (a turn that paused or stopped): a line of their own. */
  const helperNote = (helpers: HelperLaneData[]): Message[] =>
    helpers.length > 0 ? [{ role: 'assistant', msgType: 'note', content: '', helpers }] : [];

  /** The team on screen after a change, kept in the ref the stream folds into. */
  const putTeam = (team: TeamState) => {
    liveTeamRef.current = team;
    setLiveTeam(team);
  };

  /** This turn's team, taken for the message it belongs with (TEAM1b); null when the lead worked alone. */
  const takeLiveTeam = (): TeamState | null => {
    const team = liveTeamRef.current;
    putTeam(emptyTeam());
    setStoppingMembers(new Set());
    setSendTo('lead');
    return hasTeam(team) ? team : null;
  };

  /** A team with no answer to sit with (a turn that paused or stopped): its lanes on a line of their own. */
  const teamNote = (team: TeamState | null, helpers: HelperLaneData[] = []): Message[] =>
    team ? [{ role: 'assistant', msgType: 'note', content: '', team, ...(helpers.length ? { helpers } : {}) }]
      : helperNote(helpers);

  /** A member's Stop: that member alone; its lead reads that it was stopped, and the rest work on. */
  const stopMember = async (member: MemberLaneData) => {
    setStoppingMembers(prev => new Set(prev).add(member.id));
    try {
      await fetch(`/api/chat/${encodeURIComponent(sessionId)}/members/${encodeURIComponent(member.id)}/stop`,
                  { method: 'POST' });
    } catch {
      // The lane says how it ended either way.
    }
  };

  /** The person's words to one working member, read between its steps; false when nobody read them. */
  const messageMember = async (member: MemberLaneData, text: string): Promise<boolean> => {
    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(sessionId)}/members/${encodeURIComponent(member.id)}/message`,
                              { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ text }) });
      const delivered = res.ok && (await res.json()).delivered === true;
      if (delivered) putTeam(personNote(liveTeamRef.current, member.name, text));
      return delivered;
    } catch {
      return false;
    }
  };

  /** The person's words to the lead while its members work, read before its next model call. */
  const messageLead = async (text: string): Promise<boolean> => {
    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(sessionId)}/lead/message`,
                              { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ text }) });
      const delivered = res.ok && (await res.json()).delivered === true;
      if (delivered) putTeam(personNote(liveTeamRef.current, 'the lead', text));
      return delivered;
    } catch {
      return false;
    }
  };

  /**
   * A paused team's question answered (TEAM1b): the team comes back on screen as live, the answered member's
   * lane says so, and the question's card stays answered rather than asked again below. The answer itself
   * resumes the turn (`answerMember`).
   */
  const resumeTeamOnScreen = (answer: MemberAnswer) => {
    const memberId = answer.member_id ?? answer.question_id.split(':')[0];
    const at = messages.findLastIndex(m => m.msgType === 'note' && m.team?.members.some(x => x.id === memberId));
    if (at === -1) return;
    putTeam(answeredHere(messages[at].team as TeamState, memberId, answer.approve));
    setMessages(prev => prev
      .map((m, i) => (i === at ? { ...m, team: undefined }
        : m.msgType === 'human_input_required' && m.data?.approval_id === answer.approval_id
          ? { ...m, data: { ...m.data, answered_in_lane: true } } : m))
      .filter((m, i) => !(i === at && !m.helpers?.length)));
  };

  /** A team at work: the composer writes to its lead or a member, and Stop stops the whole team. */
  const teamAtWork = isLoading && hasTeam(liveTeam) && !liveTeam.ended;
  const teamTargets = useMemo(() => (teamAtWork ? workingMembers(liveTeam) : []), [teamAtWork, liveTeam]);
  useEffect(() => {
    // A member that finished is no longer one to write to.
    if (sendTo !== 'lead' && !teamTargets.some(m => m.id === sendTo)) setSendTo('lead');
  }, [sendTo, teamTargets]);
  useEffect(() => { if (!teamAtWork) setTeamSendNote(null); }, [teamAtWork]);

  /** The composer's words to the team at work: to the lead, or the member picked in "To". */
  const sendToTeam = async () => {
    const text = input.trim();
    if (!text) return;
    const member = teamTargets.find(m => m.id === sendTo);
    const delivered = member ? await messageMember(member, text) : await messageLead(text);
    const who = member ? member.name : 'the lead';
    if (delivered) setInput('');
    setTeamSendNote(delivered ? `Sent to ${who}. It reads it before its next step.`
                              : `${member ? member.name : 'The team'} had already finished; not sent.`);
  };

  /** The questions a paused team's lanes answer here: the ones the turn paused on, while nothing runs. */
  const parkedMemberQuestions = (): Set<string> => {
    const last = messages[messages.length - 1];
    if (isLoading || !last || last.msgType !== 'human_input_required' || !last.data?.member_id) return new Set();
    // TEAM1c's list of every question the turn paused on, or the one it carries.
    const listed = Array.isArray(last.data.questions) ? (last.data.questions as TeamQuestion[]) : [];
    return new Set(listed.length ? listed.map(q => q.question_id)
      : [String(last.data.question_id ?? `${last.data.member_id}:${last.data.approval_id ?? ''}`)]);
  };

  /** A lane's Stop: that helper alone, whether its agent waits for it or it works in the background. */
  const stopHelper = async (helper: HelperLaneData) => {
    setStoppingHelpers(prev => new Set(prev).add(helper.id));
    try {
      await fetch(`/api/chat/${encodeURIComponent(sessionId)}/helpers/${encodeURIComponent(helper.id)}/stop`,
                  { method: 'POST' });
    } catch {
      // The lane says how it ended either way.
    }
  };

  /** "Save as agent" (S9): a new agent opened in the editor, made from the helper; nothing saved until Save. */
  const saveHelperAsAgent = (helper: HelperLaneData) => {
    if (putAgentDraft(agentFromHelper(helper))) router.push('/agents?draft=1');
  };

  /**
   * A background helper's lane, followed on its own conversation's stream until it ends;
   * then whatever it posted (a follow-up turn, watched as it is written) or its record as saved.
   * A turn of the person's that is running reads the result itself, so the page leaves it be.
   */
  const followHelper = async (lane: HelperLaneData, sid: string) => {
    let current = lane;
    try {
      const res = await fetch(`/api/chat/${encodeURIComponent(lane.sessionId ?? '')}/events`);
      if (res.ok && res.body) {
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let cut: number;
          while ((cut = buffer.indexOf('\n\n')) !== -1) {
            const block = buffer.slice(0, cut);
            buffer = buffer.slice(cut + 2);
            for (const line of block.split('\n')) {
              if (!line.startsWith('data: ')) continue;
              try {
                const event = JSON.parse(line.slice(6));
                if (!isHelperEvent(event)) continue;
                current = foldHelpers([current], event)[0] ?? current;
                setBackgroundLanes(prev => ({ ...prev, [current.id]: current }));
              } catch {
                // not an event
              }
            }
          }
        }
      }
    } catch {
      // Gone: the conversation below says how it ended.
    }
    const epoch = conversationEpochRef.current;
    const agentId = currentAgentIdRef.current;
    const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : '';
    for (let look = 0; look < 15; look++) {
      await new Promise(resolve => setTimeout(resolve, look === 0 ? 300 : 700));
      if (epoch !== conversationEpochRef.current || streamActiveRef.current) return;
      const since = turnsStartedRef.current;
      const res = await fetch(`/api/sessions/${encodeURIComponent(sid)}/history${query}`).catch(() => null);
      if (!res?.ok) continue;
      const data = await res.json();
      const posted = holdsHelper(data.running?.automated, lane.id)
        || (data.turns ?? []).some((t: SessionTurn) => holdsHelper(t.automated, lane.id));
      if (posted) {
        await reopenConversation(sid, agentId, epoch, since);
        return;
      }
    }
    if (epoch === conversationEpochRef.current && !streamActiveRef.current) await loadConversation(sid, agentId, epoch);
  };

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

  /** Run the last question again, once, on the stronger model its failed turn was offered (F1018). */
  const handleTryStronger = async () => {
    const payload = lastSentRef.current;
    const offer = strongerOffer;
    if (!payload || !offer || isLoading) return;
    await popStoredTurn(payload.content);
    setMessages(prev => {
      const idx = prev.findLastIndex(m => m.role === 'user');
      return idx === -1 ? prev : prev.slice(0, idx + 1);
    });
    await processMessage(payload.content, payload.images, offer.retry_of);
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
   * A browser turn handed back (BRW2, F1077), from its card or from the pane (Done, or letting a held send through,
   * or allowing a site): the engine goes on in a follow-up turn of its own, and this reads it, as a turn whose stream
   * was lost is read again. Once per turn.
   */
  const resumeAfterBrowser = async () => {
    if (isLoading) return;
    const parked = messages.findLastIndex(m => m.msgType === 'human_input_required' && m.data?.kind === 'browser_turn');
    if (parked === -1 || messages.slice(parked + 1).some(m => m.role === 'user' || m.data?.handedBack)) return;
    const question = [...messages].reverse().find(m => m.role === 'user')?.content ?? '';
    setMessages(prev => [...prev, { role: 'assistant', msgType: 'note', content: 'You handed the browser back.',
                                    data: { handedBack: true } }]);
    setIsLoading(true);
    setDraftAnswer('');
    setTurnOutcome(null);
    turnAnsweredRef.current = false;
    // A new turn (BRW1): the engine follows the hand-back with a turn of its own, and the paused one's cursor is
    // gone (410), so it is read from its start.
    lastCursorRef.current = null;
    lastThoughtRef.current = null;
    refusedRef.current = [];
    liveCallsRef.current = [];
    sourcesRef.current = null;
    notesRef.current = [];
    try {
      await recoverTurn(sessionId, question);
    } catch (e) {
      if ((e as { engineError?: boolean })?.engineError) {
        setTurnOutcome({ kind: 'failed', message: (e as Error).message,
                         model: Boolean((e as Error & { modelError?: boolean }).modelError) });
      }
    } finally {
      setIsLoading(false);
      setStreamingActivity(null);
      setIsThinking(false);
      setThinkingPreview(null);
    }
  };
  // Given again each render, so the shell always calls the one that sees this render's conversation.
  useEffect(() => { onResumeHook(() => { void resumeAfterBrowser(); }); });

  /**
   * Allow or refuse a tool call the agent stopped to ask about.
   *
   * The answer resumes the same turn — the backend picks it up from its last
   * turn-boundary snapshot — so this streams into the transcript exactly as
   * the first half did, rather than starting a new exchange.
   */
  const handleToolApproval = async (approve: boolean, reason: string, approvalId?: string, questionId?: string) => {
    if (isLoading) return;
    // The question the card belongs to — what the turn will be saved under.
    const question = [...messages].reverse().find(m => m.role === 'user')?.content ?? '';
    setMessages(prev => [...prev, {
      role: 'assistant', msgType: 'note',
      content: approve ? 'You allowed the call.' : `You refused the call${reason ? `: ${reason}` : '.'}`,
    }]);
    setIsLoading(true);
    setDraftAnswer('');
    setTurnOutcome(null);
    turnAnsweredRef.current = false;
    lastCursorRef.current = null;
    lastThoughtRef.current = null;
    refusedRef.current = [];
    liveCallsRef.current = [];
    sourcesRef.current = null;
    notesRef.current = [];
    const controller = new AbortController();
    sseAbortRef.current = controller;
    try {
      const response = await fetch(`/api/chat/${encodeURIComponent(sessionId)}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // Naming the question: a card left open in another tab must not answer
        // one asked after it (F247).
        body: JSON.stringify({ approve, reason, agent_id: currentAgentId, approval_id: approvalId,
                               question_id: questionId }),
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

  /**
   * Answer one team member's question (TEAM1c, Q6). While the turn is still being written,
   * the answer reaches that member at once — or is held for the turn as it starts or pauses —
   * and this turn's own stream shows the rest (`member_answered`). Once the turn has paused,
   * the answer resumes it, as any approval does.
   */
  const answerMember = async (answer: MemberAnswer): Promise<'answered' | 'sent' | 'failed'> => {
    if (!isLoading) {
      resumeTeamOnScreen(answer);
      await handleToolApproval(answer.approve, answer.reason, answer.approval_id, answer.question_id);
      return 'answered';
    }
    try {
      const response = await fetch(`/api/chat/${encodeURIComponent(sessionId)}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approve: answer.approve, reason: answer.reason, agent_id: currentAgentId,
                               approval_id: answer.approval_id, question_id: answer.question_id }),
      });
      if (!response.ok) return 'failed';
      const said = (await response.json()) as { status?: string };
      return said.status === 'sent' ? 'sent' : 'answered';
    } catch {
      return 'failed';
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
    turnsStartedRef.current += 1;
    lastSentRef.current = { content: question, images };
    turnAnsweredRef.current = false;
    lastCursorRef.current = null;
    setTurnOutcome(null);
    setStrongerOffer(null);
    setIsLoading(true);
    setDraftAnswer('');
    setStreamingActivity(null);
    setIsThinking(false);
    setThinkingPreview(null);
    pendingThoughtsRef.current = [];
    pendingReasoningRef.current = [];
    lastToolRef.current = null;
    lastThoughtRef.current = null;
    refusedRef.current = [];
    liveCallsRef.current = [];
    liveHelpersRef.current = [];
    setLiveHelpers([]);
    putTeam(emptyTeam());
    setStoppingMembers(new Set());
    setSendTo('lead');
    automatedTurnRef.current = false;
    sourcesRef.current = null;
    notesRef.current = [];
    stepSourcesRef.current = {};
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
      lastThoughtRef.current = null;
      // The conversation carries on after a workflow answers, as it does after
      // any agent (F260): a new session here threw it away, so the workflow's
      // next run could not read what was said. It began as a guard against
      // step agents' own turns leaking into the next message.
    }
  };

  const processMessage = (content: string, images?: string[], retryOf?: string) => inTurn(content, images, async () => {
    try {
      await processMessageSSE(content, images, retryOf);
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
  const watchTurn = (sid: string, cursor: string, question: string, automated = false) => inTurn(question, undefined, async () => {
    automatedTurnRef.current = automated;
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
    if (isHelperEvent(data)) {
      // A helper's own work (SUB1a): its lane, never the agent's activity line or its answer.
      liveHelpersRef.current = foldHelpers(liveHelpersRef.current, data);
      setLiveHelpers(liveHelpersRef.current);
      return { done: false };
    }
    if (isTeamEvent(data)) {
      // A Team agent's members and task list (TEAM1b): their lanes, never the lead's activity line or answer.
      putTeam(foldTeam(liveTeamRef.current, data));
      return { done: false };
    }
    if (data.type === 'tool_execution' && hasTeam(liveTeamRef.current)) {
      // The lead's message to a member is a note as well as its call.
      putTeam(foldTeam(liveTeamRef.current, data));
    }
    switch (data.type) {

      // ── Standard events ────────────────────────────────────────────────────
      case 'status':
        setStreamingActivity(data.message);
        setIsThinking(false);
        break;

      case 'thinking':
        setIsThinking(true);
        // Progress messages ("Analyzing your request…", "Delegating to X…")
        // used to be discarded — surface them in the thinking line. The loop's
        // own per-turn "Analyzing your request…" no longer covers a thought the
        // agent has already had: it arrives before every model call, so the
        // line said nothing else all turn (U57).
        if (data.message && !(data.message === GENERIC_THINKING && lastThoughtRef.current)) {
          setThinkingPreview(`${delegatePrefix(data)}${String(data.message)}`);
        }
        break;

      case 'tool_execution': {
        lastToolRef.current = data.tool_name as string;
        if (!data.orch_step_id && !data.delegate_from) {
          const args = data.args && typeof data.args === 'object' ? data.args as Record<string, unknown> : null;
          liveCallsRef.current = [...liveCallsRef.current, { tool: String(data.tool_name), args, result: '' }];
        }
        const toolDisplayName = (data.tool_name as string)
          .replace(/_/g, ' ')
          .replace(/\b\w/g, (l: string) => l.toUpperCase());
        const stepLabel = data.step_name ? ` · ${data.step_name}` : '';
        // A Team agent's own moves read as what it is doing, not as tools (TEAM1b).
        const leadMove = !data.delegate_from && !data.orch_step_id
          && (hasTeam(liveTeamRef.current) || data.tool_name === 'assign') ? teamActivity(String(data.tool_name)) : null;
        setStreamingActivity(leadMove ?? `🔧 ${delegatePrefix(data)}${toolDisplayName}${stepLabel}`);
        // The thought that chose this tool stays under it while it runs (U57).
        setIsThinking(Boolean(lastThoughtRef.current));
        break;
      }

      // The browser inside Synapse (BRW1): the pane opens on the session, and each action joins its call's line.
      case 'browser_session':
        if (isBrowserEvent(data)) onBrowser(data, agentNameRef.current);
        break;

      case 'browser_action': {
        if (!isBrowserEvent(data)) break;
        const action = actionOf(data);
        const calls = liveCallsRef.current;
        const at = calls.findLastIndex(c => c.tool === action.action && !c.browser);
        if (at !== -1) {
          const next = [...calls];
          next[at] = { ...next[at], browser: action };
          liveCallsRef.current = next;
        }
        onBrowser(data, agentNameRef.current);
        break;
      }

      case 'tool_result': {
        const rawToolName = data.tool_name as string || 'Tool';
        if (!data.orch_step_id && !data.delegate_from) {
          // The newest call to this tool still without its result.
          const calls = liveCallsRef.current;
          const at = calls.findLastIndex(c => c.tool === rawToolName && !c.result && !c.refused);
          if (at !== -1) {
            const next = [...calls];
            next[at] = {
              ...next[at],
              result: String(data.preview ?? ''),
              ...(data.refused ? { refused: true, reason: String(data.reason ?? '') } : {}),
            };
            liveCallsRef.current = next;
          }
        }
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
        if (data.refused) {
          // Not run (U68): a refused call used to get the same tick as one that ran.
          if (!data.orch_step_id && !data.delegate_from) {
            refusedRef.current = [...refusedRef.current, { tool: rawToolName, reason: String(data.reason ?? '') }];
          }
          setStreamingActivity(`⛔ ${delegatePrefix(data)}${resultToolName} — not run`);
          setIsThinking(Boolean(lastThoughtRef.current));
          break;
        }
        // A Team agent's own move done: it reads as what the lead does next, not as a tool's tick (TEAM1b).
        const teamMoveDone = !data.delegate_from && !data.orch_step_id && hasTeam(liveTeamRef.current)
          && teamActivity(rawToolName) !== null;
        setStreamingActivity(teamMoveDone ? 'Planning its next step'
          : `✓ ${delegatePrefix(data)}${resultToolName}${subtaskSuffix}`);
        setIsThinking(Boolean(lastThoughtRef.current));
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
          const line = thoughtLine(String(data.reasoning));
          if (line) {
            lastThoughtRef.current = line;
            setThinkingPreview(`${delegatePrefix(data)}${line}`);
          }
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
        } else if (!data.delegate_from) {
          // A delegate's thought is its own, not this agent's (F547): it shows in the line below only.
          pendingThoughtsRef.current = [...pendingThoughtsRef.current, data.thought as string];
        }
        {
          // A model that thinks in prose rather than in a reasoning block says
          // what it is doing here; a tool call's JSON is not a thought (U57).
          const line = thoughtLine(String(data.thought ?? ''), true);
          if (line) {
            lastThoughtRef.current = line;
            setThinkingPreview(`${delegatePrefix(data)}${line}`);
          }
        }
        setIsThinking(true);
        break;

      case 'answer_delta':
        // Only the agent's own answer: a delegate's or a workflow step's is not.
        if (!data.orch_step_id && !data.delegate_from) {
          setDraftAnswer(prev => prev + String(data.text ?? ''));
        }
        break;

      case 'answer_reset':
        // What streamed was not the answer — narration before a tool call, which
        // the thinking line now carries, or a call sent again.
        if (!data.orch_step_id && !data.delegate_from) setDraftAnswer('');
        break;

      case 'response': {
        turnAnsweredRef.current = true;
        setDraftAnswer('');
        const capturedThoughts = withoutEcho([...pendingThoughtsRef.current], data.content);
        const capturedReasoning = [...pendingReasoningRef.current];
        const refused = refusedRef.current;
        refusedRef.current = [];
        const toolCalls = liveCallsRef.current;
        liveCallsRef.current = [];
        const sources = sourcesRef.current;
        sourcesRef.current = null;
        const notes = notesRef.current;
        notesRef.current = [];
        const helpers = takeLiveHelpers();
        const answered = foldTeam(liveTeamRef.current, data);
        takeLiveTeam();
        const team = answered.summary || hasTeam(answered) ? answered : null;
        setMessages(prev => [...answerOnce(prev, data.content as string), {
          role: 'assistant',
          content: data.content as string,
          ...(helpers.length > 0 ? { helpers } : {}),
          ...(team ? { team } : {}),
          intent: data.intent,
          data: data.data,
          tool: data.tool_name,
          thoughts: capturedThoughts,
          reasoning: capturedReasoning,
          ...(refused.length > 0 ? { refused } : {}),
          ...(toolCalls.length > 0 ? { toolCalls } : {}),
          ...(sources ?? {}),
          ...(notes.length > 0 ? { notes } : {}),
        }]);
        pendingThoughtsRef.current = [];
        pendingReasoningRef.current = [];
        lastThoughtRef.current = null;
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
            ...(stepSourcesRef.current[data.orch_step_id as string] ?? {}),
          }]);
          stepState.thoughts = [];
          stepState.reasoning = [];
          stepState.receivedAgentStep = true;
        } else {
          // Top-level final (single-agent response outside orchestration)
          const capturedThoughts = withoutEcho([...pendingThoughtsRef.current], data.response);
          const capturedReasoning = [...pendingReasoningRef.current];
          const refused = refusedRef.current;
          refusedRef.current = [];
          const toolCalls = liveCallsRef.current;
          liveCallsRef.current = [];
          const sources = sourcesRef.current;
          sourcesRef.current = null;
          const notes = notesRef.current;
          notesRef.current = [];
          const helpers = takeLiveHelpers();
          const answered = foldTeam(liveTeamRef.current, data);
          takeLiveTeam();
          const team = answered.summary || hasTeam(answered) ? answered : null;
          setMessages(prev => [...answerOnce(prev, data.response as string), {
            role: 'assistant',
            content: data.response as string,
            ...(helpers.length > 0 ? { helpers } : {}),
            ...(team ? { team } : {}),
            intent: data.intent,
            data: data.data,
            tool: data.tool_name,
            thoughts: capturedThoughts,
            reasoning: capturedReasoning,
            ...(refused.length > 0 ? { refused } : {}),
            ...(toolCalls.length > 0 ? { toolCalls } : {}),
            ...(sources ?? {}),
            ...(notes.length > 0 ? { notes } : {}),
          }]);
          pendingThoughtsRef.current = [];
          pendingReasoningRef.current = [];
          lastThoughtRef.current = null;
        }
        break;
      }

      // ── Orchestration lifecycle ────────────────────────────────────────────
      case 'orchestration_start':
        setStreamingActivity(`🚀 Starting workflow`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `🚀 ${(data.orchestration_name as string) || 'Workflow'} started`,
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

      case 'agent_step_result': {
        stepState.receivedAgentStep = true;
        const stepSources = stepSourcesRef.current[data.orch_step_id as string];
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
          ...(stepSources ?? {}),
        }]);
        stepState.thoughts = [];
        stepState.reasoning = [];
        break;
      }

      case 'knowledge_citations': {
        // The answer's [K#] labels resolved (B1b). Its own turn's only: a
        // delegate's labels are resolved in its lead's answer.
        if (data.delegate_from) break;
        const sources = {
          citations: (data.citations as KnowledgeCitation[] | undefined) ?? [],
          unresolvedCitations: (data.unresolved as string[] | undefined) ?? [],
        };
        if (data.orch_step_id) stepSourcesRef.current[data.orch_step_id as string] = sources;
        else sourcesRef.current = sources;
        break;
      }

      case 'step_warning':
        // The turn's own warning (F582: a card it left claimed), kept under its answer (F584). A step's is its
        // run's, read on the run page; a delegate's is its lead's to say.
        if (!data.orch_step_id && !data.delegate_from && typeof data.message === 'string' && data.message) {
          notesRef.current = [...notesRef.current, data.message];
        }
        break;

      case 'step_complete':
        setStreamingActivity(`✓ ${(data.step_name as string) || 'Step'} complete`);
        break;

      case 'step_error':
        setStreamingActivity(`✗ Step failed`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          // The step's own words when the stream carries them (F195); the
          // sentence in `error` is what a reader that cannot see them gets.
          content: `❌ Step failed: ${(data.reason as string) || (data.error as string) || 'Unknown error'}`,
          msgType: 'orchestration_complete',
        }]);
        break;

      case 'orchestration_complete':
        setStreamingActivity(`Workflow ${data.status as string}`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `✅ Workflow ${(data.status as string) || 'complete'}`,
          msgType: 'orchestration_complete',
        }]);
        // No state here, ever (U67). The run's answer arrives as its own
        // `final`; drawing `final_state` put every key — USER_INPUT,
        // CATEGORY, HUMAN_RESPONSE — on a chat user's screen as a card.
        break;

      case 'orchestration_error':
        setStreamingActivity(`Workflow error`);
        setMessages(prev => [...prev, {
          role: 'assistant',
          content: `❌ Workflow error: ${(data.error as string) || 'Unknown error'}`,
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
        // A member's question (TEAM1b): drawn in its lane, which the team's note keeps on screen.
        if (data.member_id) putTeam(foldTeam(liveTeamRef.current, data));
        const kept = teamNote(takeLiveTeam(), takeLiveHelpers());
        setMessages(prev => [...prev, ...kept, {
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
            // A browser turn (BRW2): the page is the person's until they hand it back.
            session_id: data.session_id,
            reason: data.reason,
            banner_kind: data.banner_kind,
            tool_name: data.tool_name,
            tool_args: data.tool_args,
            category: data.category,
            approval_id: data.approval_id,
            // A team member's (TEAM1c): which member asked, and every question the turn paused on.
            delegate_from: data.delegate_from,
            delegate_agent_name: data.delegate_agent_name,
            member_id: data.member_id,
            question_id: data.question_id,
            questions: data.questions,
            // Its lane answers it (TEAM1b): the member's name, beside TEAM1c's ids.
            member_name: data.delegate_agent_name,
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

      case 'done': {
        // A turn that ended with no answer (stopped, failed) keeps its helpers' lanes.
        const helpers = takeLiveHelpers();
        const team = takeLiveTeam();
        if (team || helpers.length > 0) setMessages(prev => [...prev, ...teamNote(team, helpers)]);
        return { done: true };
      }

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

      case 'try_stronger':
        // Sent before the turn's error, which ends the reading (F1018).
        if (typeof data.retry_of === 'string' && typeof data.label === 'string') {
          setStrongerOffer({ label: data.label, retry_of: data.retry_of });
        }
        return { done: false };
    }
    return { done: false };
  };

  // SSE Streaming implementation
  const processMessageSSE = async (content: string, images?: string[], retryOf?: string) => {
    const controller = new AbortController();
    sseAbortRef.current = controller;
    streamActiveRef.current = true;
    lastChunkAtRef.current = Date.now();
    try {
      const response = await fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: content, session_id: sessionId, agent_id: currentAgentId, images: images || [],
                               ...(retryOf ? { retry_of: retryOf } : {}) }),
        signal: controller.signal,
      });
      if (response.status === 409) {
        throw Object.assign(new Error('A reply is still being written in this conversation.'), { turnRunning: true });
      }
      if (response.status === 422) {
        // Refused before it started — images this agent would not read (F396),
        // or a body the route will not take. Its sentence is the answer, and
        // reading the conversation again would find no turn to recover.
        const detail = await response.json().then((b) => b?.detail, () => null);
        throw Object.assign(new Error(typeof detail === 'string' && detail ? detail : 'The message was refused.'),
          { engineError: true });
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

    setIsLoading(true);
    setDraftAnswer('');
    setStreamingActivity('Resuming workflow...');
    setIsThinking(false);
    pendingThoughtsRef.current = [];
    pendingReasoningRef.current = [];
    lastThoughtRef.current = null;

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
      // Drawn once accepted, never before (BRW2, F1078): a refused answer left the person's words on screen (a card
      // number the box held back, for one) and the question read as answered.
      setMessages(prev => [...prev, { role: 'user', content: userMessage }]);

      const reader = response.body?.getReader();
      if (!reader) throw new Error('No reader available');

      const decoder = new TextDecoder();
      const stepState = { thoughts: [] as string[], reasoning: [] as string[], orchStepId: null as string | null, receivedAgentStep: false };
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
      setMessages(prev => [...prev, { role: 'assistant', content: 'Error resuming the workflow.' }]);
    } finally {
      setIsLoading(false);
      setStreamingActivity(null);
      setIsThinking(false);
      pendingThoughtsRef.current = [];
      pendingReasoningRef.current = [];
      lastThoughtRef.current = null;
    }
  };

  // ─── Rendering helpers ─────────────────────────────────────────────────────

  /** A message's helpers' lanes (SUB1a), a background one as its own stream has it since. */
  const lanesOf = (msg: Message) => msg.helpers?.length ? (
    <HelperLanes
      helpers={msg.helpers.map(lane => backgroundLanes[lane.id] ?? lane)}
      onStop={stopHelper}
      onSaveAsAgent={saveHelperAsAgent}
      stopping={stoppingHelpers}
      className="mb-2"
    />
  ) : null;

  /**
   * A message's team (TEAM1b): its task list and each member as one line that opens. A turn paused on a
   * member's question keeps its lanes open, with the question answerable in that member's lane.
   */
  const teamOf = (msg: Message) => {
    if (!hasTeam(msg.team)) return null;
    const parked = parkedMemberQuestions();
    const answerable = msg.team.members.some(m => m.question && !m.question.answered
      && parked.has(m.question.questionId));
    return (
      <TeamView
        state={msg.team}
        budget={currentDetails?.taskBudget}
        kept
        onAnswer={answerable ? (answer) => { void answerMember(answer); } : undefined}
        className="mb-2"
      />
    );
  };

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

    // ── A workflow step's result ──
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
    if (msg.msgType === 'note' && (msg.helpers?.length || hasTeam(msg.team))) {
      return <div key={idx} className="pl-10">{teamOf(msg)}{lanesOf(msg)}</div>;
    }
    if (msg.msgType === 'note') {
      return (
        <div key={idx} className="flex items-center gap-2 py-0.5 pl-10 text-xs text-text-faint">
          <span className="h-px w-4 shrink-0 bg-border" aria-hidden />
          <span className="min-w-0 break-words">{msg.content}</span>
        </div>
      );
    }

    // ── A tool call waiting on permission ──
    // Not a form: the answer is yes or no, and the interesting part is *what*
    // is being asked, so the call and its arguments are the content.
    if (msg.msgType === 'human_input_required' && msg.data?.kind === 'tool_approval') {
      // A team member's question sits in that member's lane (TEAM1b): the card would ask it twice.
      const inLane = msg.data?.member_id && (msg.data?.answered_in_lane || messages.slice(0, idx).some(m => m.team?.members.some(
        x => x.id === msg.data?.member_id && x.question?.approvalId === msg.data?.approval_id)));
      if (inLane) return null;
      return (
        <ToolApprovalCard
          key={idx}
          msg={msg}
          onDecide={(approve, reason) => (msg.data?.question_id
            ? void answerMember({ approve, reason, approval_id: msg.data?.approval_id,
                                  question_id: msg.data.question_id })
            : handleToolApproval(approve, reason, msg.data?.approval_id))}
          disabled={isLoading}
          movedPast={messages.slice(idx + 1).some(m => m.role === 'user')}
        />
      );
    }

    // ── A browser turn: the page is the person's for something only they should do (BRW2, F1077) ──
    // A workflow's step parked the same way carries it in its fields (BRW1: the field's type says).
    const runTurn = msg.msgType === 'human_input_required' ? browserTurnOf(msg.data?.fields) : null;
    if (msg.msgType === 'human_input_required' && (msg.data?.kind === 'browser_turn' || runTurn)) {
      const sid = String(runTurn?.session_id ?? msg.data?.session_id ?? '');
      const answered = messages.slice(idx + 1).some(m => m.role === 'user' || m.data?.handedBack);
      return (
        <AgentTurn key={idx} avatar={<AgentAvatar className="bg-person-subtle text-person-ink"><User className="h-4 w-4" aria-hidden /></AgentAvatar>}>
          <BrowserTurnCard
            reason={String(runTurn?.reason || msg.data?.reason || msg.content)}
            kind={runTurn?.banner_kind ?? msg.data?.banner_kind}
            answered={answered}
            onOpen={sid ? () => onShowBrowser(sid, agentName) : undefined}
            onDone={sid ? async () => {
              const ok = await OSS_BROWSER_HOST.handBack!(sid);
              if (!ok) return false;
              // A chat's turn goes on in a follow-up turn, read here; a workflow's run goes on in its own step.
              if (runTurn) {
                setMessages(prev => [...prev, { role: 'assistant', msgType: 'note', data: { handedBack: true },
                                                content: 'You handed the browser back. The run goes on.' }]);
              } else void resumeAfterBrowser();
              return true;
            } : undefined}
          />
        </AgentTurn>
      );
    }

    // ── Human input required ──
    if (msg.msgType === 'human_input_required') {
      return (
        <AgentTurn key={idx} avatar={<AgentAvatar className="bg-person-subtle text-person-ink"><User className="h-4 w-4" aria-hidden /></AgentAvatar>}>
          <div className="flex min-w-0 flex-col gap-3 rounded-xl border border-person/60 bg-surface px-4 py-3.5">
            <span className="inline-flex items-center gap-1.5 text-xs font-medium text-person-ink">
              <span aria-hidden className="size-2 rounded-full bg-person" />
              Human Input Required
            </span>
            <Markdown breaks className="text-[15px] leading-relaxed">{msg.content}</Markdown>
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
              <div className="text-xs text-text-faint">
                📲 Notification sent to messaging channel
              </div>
            )}
          </div>
        </AgentTurn>
      );
    }

    // ── A helper's result, posted back: not your message (SUB1a) ──
    if (msg.role === 'user' && msg.data?.automated) {
      const automated = msg.data.automated as Automated;
      return (
        <div key={idx} className="pl-10">
          <HelperNote names={automated.names?.length ? automated.names : [automated.name || 'Helper']} />
        </div>
      );
    }

    // ── Your message ──
    if (msg.role === 'user') {
      return (
        <UserBubble
          key={idx}
          actions={
            // Rewrite this question. Focusable, so it can be reached without a
            // pointer, and only on the *last* question — editing an earlier one
            // would drop every answer after it, a bigger action than a pencil implies.
            !isLoading && idx === messages.length - 2 ? (
              <button
                type="button"
                onClick={() => handleEditMessage(idx)}
                aria-label="Edit this message and send it again"
                title="Edit and send again"
                className="rounded-md p-1.5 text-text-faint opacity-0 transition-opacity hover:bg-surface-2 hover:text-text focus-visible:opacity-100 group-hover:opacity-100"
              >
                <Pencil className="h-3.5 w-3.5" aria-hidden />
              </button>
            ) : undefined
          }
        >
          <Markdown className="text-[15px] leading-relaxed">{msg.content}</Markdown>
          {!msg.images?.length && Array.isArray(msg.data?.imageNotes) && msg.data.imageNotes.length > 0 && (
            <ImageNoteChip notes={msg.data.imageNotes as { type: string }[]} />
          )}
          {msg.images && msg.images.length > 0 && <ImageThumbnails images={msg.images} />}
        </UserBubble>
      );
    }

    // ── The agent's answer ──
    return (
      <AgentTurn key={idx} className="group" avatar={<AgentAvatar><Bot className="h-4 w-4" aria-hidden /></AgentAvatar>} aside={<CopyAnswer text={msg.content} />}>
        {teamOf(msg)}
        {lanesOf(msg)}
        <AssistantBubble msg={msg} agentName={agentName} />
        {/* Dynamic UI based on Intent - Rendered Outside the answer */}
        {msg.intent === 'render_local_file' && (
          <div className="max-h-96 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface-2 p-4 font-code text-xs text-text">
            {msg.data.content}
          </div>
        )}
        {msg.intent === 'collect_data' && msg.data && (
          <CollectDataForm data={msg.data} onSubmit={handleCollectDataSubmit} />
        )}
      </AgentTurn>
    );
  };

  // The keys, under the composer, until the conversation has a first message (UX1: the hint never went away).
  const firstMessageSent = messages.some(m => m.role === 'user');
  const provider = systemStatus?.provider
    ? systemStatus.provider.charAt(0).toUpperCase() + systemStatus.provider.slice(1)
    : null;
  // Whether the agent can answer (F182) — a workflow agent's steps, each checked (F313) — where the page's
  // header said it before the tabs took the header's place: its title is the problem, when there is one.
  const readyDot = systemStatus ? (
    <span
      data-model-dot=""
      className={cn(
        "size-2 shrink-0 rounded-full",
        systemStatus.model_ready === true ? "bg-success" : systemStatus.model_ready === false ? "bg-warning" : "bg-text-faint",
      )}
      title={systemStatus.model_problem || undefined}
      aria-hidden
    />
  ) : null;
  const modelLabel = workflowId ? (
    // A workflow: each step runs on the model set on it (U64).
    <span className="flex min-w-0 items-center gap-1.5" title="A workflow: each step runs on the model set on it">
      {readyDot}
      <span className="truncate">
        Workflow{workflowStepCount !== null && <> · {workflowStepCount} {workflowStepCount === 1 ? 'step' : 'steps'}</>}
      </span>
    </span>
  ) : systemStatus ? (
    <span className="flex min-w-0 items-center gap-1.5" title={[provider, systemStatus.model].filter(Boolean).join(' · ')}>
      {readyDot}
      <span className="truncate">
        {provider && <span className="hidden sm:inline">{provider} · </span>}{systemStatus.model || 'Loading...'}
      </span>
    </span>
  ) : null;

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      <div className="relative flex w-full flex-1 flex-col">

        {/* Synapse AI, the default agent (ASSIST1, D291): its own conversation in place, the panel's, with its
            cards; the composer's pill is still the agent picker. Any other agent is this page's chat. */}
        {isAssistant ? (
          <AssistantTab
            tab={tab}
            onChange={(edit) => onChangeRef.current(edit)}
            onBusy={(busy) => onBusyRef.current(busy)}
            onInput={setInput}
            onOpenChat={(id, title) => onOpenChatRef.current(id, tab.agentId, title)}
            pill={(
              <AgentPicker
                agents={systemStatus?.agents
                  ? Object.entries(systemStatus.agents).filter(([id]) => !id.startsWith('agent_native_builder'))
                  : []}
                activeId={currentAgentId}
                activeName={agentName}
                onPick={handleSwitchAgent}
              />
            )}
          />
        ) : (<>
        {/* Chat Area */}
        <div className="custom-scrollbar relative flex-1 overflow-y-auto scroll-smooth">

          {messages.length === 0 && !isLoading && (opening || openFailed) ? (
            /* The conversation is being read back, or the read failed: never the empty
               state, which reads as a conversation with nothing in it (F873's class). */
            <div role="status" className="flex h-full flex-col items-center justify-center gap-3 px-6 text-sm text-text-muted">
              {openFailed ? (
                <>
                  <span>This conversation couldn&apos;t be opened.</span>
                  <Button type="button" variant="secondary" size="sm" onClick={() => { void initAgent(); }}>
                    <RotateCcw className="h-3 w-3" aria-hidden />
                    Try again
                  </Button>
                </>
              ) : (
                <>
                  <Spinner size={20} />
                  <span>Opening the conversation…</span>
                </>
              )}
            </div>
          ) : messages.length === 0 && !isLoading ? (
            /* ── Welcome Screen ── */
            <WelcomeScreen
              agentName={agentName}
              modelReady={systemStatus?.model_ready}
              modelProblem={systemStatus?.model_problem}
              isWorkflow={!!workflowId}
              model={modelLabel}
              intro={agentIntro}
              onNavigate={(path) => router.push(path)}
              showExamplesBanner={showExamplesBanner}
              onDismissBanner={() => setShowExamplesBanner(false)}
              onConnectModel={modelSetup.open}
            />
          ) : (
            <div className="px-4 pb-44 pt-6 md:px-6">
              <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
                {messages.map((msg, idx) => renderMessage(msg, idx))}

                {/* Working: the mark in the avatar's slot, at the avatar's size,
                    and the activity in words beside it. */}
                {isLoading && (
                  <AgentTurn working>
                    {hasTeam(liveTeam) && (
                      <TeamView
                        state={liveTeam}
                        budget={currentDetails?.taskBudget}
                        onStop={stopMember}
                        onMessage={messageMember}
                        onAnswer={(answer) => { void answerMember(answer); }}
                        stopping={stoppingMembers}
                        className="mb-3"
                      />
                    )}
                    {liveHelpers.length > 0 && (
                      <HelperLanes helpers={liveHelpers} onStop={stopHelper} stopping={stoppingHelpers} className="mb-2" />
                    )}
                    {draftAnswer ? (
                      // The answer arriving: words, not a status line about them.
                      <div data-answer-draft className="min-w-0 max-w-full">
                        <Markdown className="text-[15px] leading-relaxed">{draftAnswer}</Markdown>
                      </div>
                    ) : (
                      <div className="flex flex-col gap-1 pt-0.5">
                        <span className="text-sm text-text-muted">
                          {streamingActivity || 'Processing'}
                        </span>
                        {isThinking && (
                          <span className="max-w-2xl truncate text-xs text-text-faint">💭 {thinkingPreview || 'Thinking'}</span>
                        )}
                      </div>
                    )}
                  </AgentTurn>
                )}

                <div ref={messagesEndRef} />
              </div>
            </div>
          )}
        </div>

        {/* Input Area. It used to be lifted clear of the AI builder's corner
            launcher (F181); the launcher is retired (M2c), so it sits at the bottom. */}
        <div data-tour="chat-composer" className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-bg via-bg to-transparent px-3 pb-4 pt-6 md:px-6 md:pb-5">
          <div className="mx-auto w-full max-w-3xl">
            {/* The turn ended without an answer: the bar says which of the two
                things happened, because they read very differently — one is the
                product failing, the other is the person deciding. */}
            {turnOutcome && !isLoading && (
              <div className={cn(
                // Wraps: at phone width the actions go under the sentence
                // rather than squeezing it to nothing.
                "mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border px-3 py-2.5 text-xs",
                turnOutcome.kind === 'failed'
                  ? "border-danger/40 bg-danger-subtle text-text"
                  : "border-border bg-surface text-text-muted",
              )}>
                {turnOutcome.kind === 'failed'
                  ? <AlertCircle className="h-3.5 w-3.5 shrink-0 text-danger" />
                  : <Square className="h-3 w-3 shrink-0 fill-current" />}
                {/* Clamped, not truncated: the provider's own words are the
                    useful part, and a one-line cut kept only the start. */}
                <span
                  className="line-clamp-3 min-w-[12rem] flex-1 break-words"
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
                      className="shrink-0 font-medium text-text underline underline-offset-2 hover:text-accent-ink"
                    >
                      Settings → Models
                    </Link>
                  )}
                  {/* A helper's follow-up has no question of yours to send again (SUB1a). */}
                  {!automatedTurnRef.current && (
                    <Button type="button" variant="secondary" size="sm" onClick={handleRetry}>
                      <RotateCcw className="h-3 w-3" aria-hidden />
                      Try again
                    </Button>
                  )}
                  <IconButton icon={X} label="Dismiss" size="sm" onClick={() => setTurnOutcome(null)} />
                </div>
              </div>
            )}

            {/* A failed turn's one offer (F1018): the same question again on a stronger model, at its price. */}
            {strongerOffer && !isLoading && !automatedTurnRef.current && (
              <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-border bg-surface px-3 py-2.5 text-xs text-text-muted">
                <span className="min-w-[12rem] flex-1">A stronger model may answer this.</span>
                <div className="ml-auto flex shrink-0 items-center gap-3">
                  <Button type="button" variant="secondary" size="sm" onClick={handleTryStronger}>
                    {strongerOffer.label}
                  </Button>
                  <IconButton icon={X} label="Dismiss" size="sm" onClick={() => setStrongerOffer(null)} />
                </div>
              </div>
            )}

            {/* Rewriting a question, rather than asking a new one. */}
            {editingIndex !== null && (
              <div className="mb-3 flex items-center gap-3 rounded-lg border border-border bg-accent-subtle px-3 py-2.5 text-xs text-text">
                <Pencil className="h-3.5 w-3.5 shrink-0 text-accent-ink" aria-hidden />
                <span className="min-w-0 flex-1">
                  Editing your message. Sending replaces it and drops the replies below it.
                </span>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => { setEditingIndex(null); setInput(''); }}
                >
                  Cancel
                </Button>
              </div>
            )}
            <AttachmentError error={attachments.error} onDismiss={attachments.dismissError} />
            <Composer
              value={input}
              onChange={type}
              // Typing ahead is allowed; sending a second turn on top of a running
              // one is not — `handleSubmit` returns early, and the draft is kept.
              onSend={() => (teamAtWork ? void sendToTeam()
                : handleSubmit({ preventDefault() {} } as unknown as React.FormEvent))}
              canSend={teamAtWork ? Boolean(input.trim()) : Boolean(input.trim()) || attachedImages.length > 0}
              sendLabel={teamAtWork ? `Send to ${teamTargets.find(m => m.id === sendTo)?.name ?? 'the lead'}`
                : editingIndex !== null ? 'Resend' : 'Send'}
              busy={isLoading}
              sendWhileBusy={teamAtWork}
              onStop={handleStop}
              stopLabel={teamAtWork ? 'Stop the team' : 'Stop the agent'}
              placeholder={teamAtWork ? `Write to ${teamTargets.find(m => m.id === sendTo)?.name ?? 'the lead'} while the team works…`
                : isLoading ? 'Agent is working — type your next message…' : `Message ${agentName}…`}
              note={teamAtWork ? teamSendNote : undefined}
              label={`Message ${agentName}`}
              autoFocus
              textareaRef={textareaRef}
              onEscape={editingIndex !== null ? () => { setEditingIndex(null); setInput(''); } : undefined}
              dropHandlers={attachments.dropHandlers}
              pasteHandlers={attachments.pasteHandlers}
              top={(
                <>
                  {showSuggested && (
                    <span className="self-start rounded-full border border-border bg-surface-2 px-2.5 py-0.5 text-xs font-medium text-text-muted">
                      {SUGGESTED_LABEL}
                    </span>
                  )}
                  <AttachmentChips images={attachedImages} onRemove={attachments.remove} />
                </>
              )}
              pill={teamAtWork ? (
                // While a team works, whom the words go to (TEAM1b, Q5): its lead or a member still working.
                <label className="flex min-w-0 items-center gap-1.5 text-xs text-text-muted">
                  To
                  <select
                    value={sendTo}
                    onChange={e => setSendTo(e.target.value)}
                    aria-label="Send to"
                    className="h-7 min-w-0 max-w-48 truncate rounded-md border border-border bg-surface px-1.5 text-xs text-text"
                  >
                    <option value="lead">The lead</option>
                    {teamTargets.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
                  </select>
                </label>
              ) : (
                // Native-builder machinery agents (ids `agent_native_builder…`) are not listed.
                <AgentPicker
                  agents={systemStatus?.agents
                    ? Object.entries(systemStatus.agents).filter(([id]) => !id.startsWith('agent_native_builder'))
                    : []}
                  activeId={currentAgentId}
                  activeName={agentName}
                  onPick={handleSwitchAgent}
                />
              )}
              attach={(
                <AttachButton
                  onFiles={attachments.accept}
                  count={attachedImages.length}
                  disabledReason={attachRefusal}
                  className="rounded-md p-1.5"
                />
              )}
              model={modelLabel}
              hint="Enter to send · Shift+Enter for a new line"
              showHint={!firstMessageSent}
            />
          </div>
        </div>
        </>)}
      </div>
    </div>
  );
}

/**
 * A Synapse AI tab: a chat of its own (TABS2, D327; F1061), drawn as the panel draws one, in place. A new tab is a
 * new chat; one opened from History, the panel's list or a pin carries that chat on. The same chat open in the panel
 * is the same conversation, one state.
 */
function AssistantTab({ tab, pill, onChange, onBusy, onInput, onOpenChat }: {
  tab: ChatTab;
  pill: React.ReactNode;
  onChange: (change: Partial<ChatTab>) => void;
  onBusy: (busy: boolean) => void;
  onInput: (text: string) => void;
  /** Another Synapse AI chat, opened or brought forward in its own tab (F1063's "Open that chat"). */
  onOpenChat: (id: string, title: string) => void;
}) {
  const chat = useBuilderChat(tab.sessionId, { fresh: Boolean(tab.fresh) });
  const onBusyRef = useRef(onBusy);
  const onChangeRef = useRef(onChange);
  useEffect(() => { onBusyRef.current = onBusy; onChangeRef.current = onChange; });
  useEffect(() => { onBusyRef.current(chat.streaming); }, [chat.streaming]);
  // The tab's title is the chat's first ask, as History's row is.
  const firstQuestion = chat.state.messages.find(m => m.role === 'user')?.content ?? '';
  useEffect(() => {
    if (firstQuestion) onChangeRef.current({ title: tabTitle(firstQuestion) });
  }, [firstQuestion]);
  // A link's suggestion stops being one once the person makes the words their own.
  const suggestion = tab.suggested ? tab.draft ?? '' : null;
  const keep = useCallback((text: string) => {
    onInput(text);
    if (suggestion !== null && text !== suggestion) onChangeRef.current({ suggested: false });
  }, [onInput, suggestion]);
  return (
    <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col">
      <BuilderConversation
        density="page"
        chat={chat}
        pill={pill}
        initialInput={tab.draft}
        suggested={Boolean(tab.suggested)}
        onInput={keep}
        onOpenChat={(id, title) => onOpenChat(id, title)}
      />
    </div>
  );
}

// ─── The tabs (TABS1, D319) ─────────────────────────────────────────────────

/** Where this browser keeps its open tabs and pins. Self-hosted has one sign-in, so one set. */
const TABS_KEY = 'synapseTabs:chat';

function readSavedTabs(): SavedChatTabs | null {
  try {
    const raw = JSON.parse(localStorage.getItem(TABS_KEY) || 'null');
    return raw ? { state: restoreChatTabs(raw), pins: restorePins(raw) } : null;
  } catch {
    return null;
  }
}

function saveTabs({ state, pins }: SavedChatTabs) {
  try {
    // A draft is kept with its tab here: this browser typed it.
    localStorage.setItem(TABS_KEY, JSON.stringify({
      tabs: state.tabs.map(({ id, sessionId, agentId, key, title, usedAt, mark, draft }) => ({
        id, sessionId, agentId, key, title, usedAt, ...(mark ? { mark } : {}), ...(draft ? { draft } : {}),
      })),
      activeId: state.activeId,
      pins,
    }));
  } catch { /* a full or blocked storage: the tabs last this page's life */ }
}

/** One status read for every open tab (`POST /api/sessions/status`). */
async function readTabStatus(tabs: ChatTab[]): Promise<Record<string, TabStatus>> {
  const res = await fetch('/api/sessions/status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessions: tabs.map(t => ({ session_id: t.sessionId, agent_id: t.agentId })) }),
  });
  if (!res.ok) return {};
  const rows: { session_id: string; agent_id: string; running: boolean; automated?: boolean; pending_approval: boolean;
                turn_count: number; last_updated: string | null }[] = (await res.json()).sessions ?? [];
  const byKey = new Map(rows.map(r => [chatKey(r.session_id, r.agent_id), r]));
  const out: Record<string, TabStatus> = {};
  for (const t of tabs) {
    const r = byKey.get(t.key);
    if (r) out[t.id] = { running: r.running, automated: !!r.automated, pendingApproval: r.pending_approval,
                         turnCount: r.turn_count, lastUpdated: r.last_updated };
  }
  return out;
}

/** The agents a tab can be opened with: Synapse AI first, never the builder's own machinery. */
function pickableAgents(status: SystemStatus | null): PickableAgent[] {
  return Object.entries(status?.agents ?? {})
    .filter(([id]) => !id.startsWith('agent_native_builder'))
    .map(([id, info]) => {
      const named = typeof info === 'object' && info !== null;
      return {
        id,
        name: named ? info.name : id,
        assistant: named && info.type === 'builder',
        note: named && info.type === 'orchestrator' ? 'A workflow' : null,
      };
    })
    .sort((a, b) => Number(b.assistant) - Number(a.assistant));
}

const onPhone = () => window.matchMedia('(max-width: 767px)').matches;

/** A tab's browser (BRW2): the session its agent opened, what it did there, and whether the pane is showing. */
type TabBrowser = {
  sessionId: string;
  agentName: string;
  actions: BrowserAction[];
  /** The pane is open beside the chat. Closing it leaves the browser running. */
  shown: boolean;
  /** The browser is closed: its last page stays, dimmed. */
  closed: boolean;
};

/** A browser event folded into the tab's browser. A turn read again replays its events, which change nothing. */
function foldBrowser(had: TabBrowser | undefined, event: BrowserEvent, agentName: string): TabBrowser | undefined {
  const fresh = (sessionId: string): TabBrowser => ({ sessionId, agentName, actions: [], shown: true, closed: false });
  if (event.type === 'browser_session') return had?.sessionId === event.session_id ? had : fresh(event.session_id);
  const action = actionOf(event);
  const base = had?.sessionId === action.session_id ? had : fresh(action.session_id);
  if (base.actions.some(a => a.n === action.n)) return had;
  return { ...base, actions: [...base.actions, action] };
}

/**
 * Chat: several conversations open at once, as tabs (TABS1, D319).
 *
 * The shell holds the tabs (`useChatTabs`, shared with cloud) and the right
 * rail's History, Pinned and Search. It mounts only the conversation in front
 * (`ChatConversation`): plain HTTP gives a page about six connections per host,
 * so only that tab streams, and the others learn how they stand from one short
 * read for all of them.
 */
export default function Home() {
  const router = useRouter();
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  // Which new-tab picker is open: the strip's, or the phone row's.
  const [picker, setPicker] = useState<'strip' | 'phone' | null>(null);
  const [panel, setPanel] = useState<RailPanel | null>(null);
  // A link to a conversation (`/?session=…&agent=…`), opened once the tabs are read.
  const link = useRef<{ sessionId: string; agentId: string } | null>(null);
  // A link to a new Synapse AI chat with words waiting (`/?assistant=new&draft=…`, TABS2): opened once, never sent.
  const suggestion = useRef<{ draft: string } | null>(null);

  const isBuilder = useCallback((agentId: string) => {
    const info = systemStatus?.agents?.[agentId];
    return typeof info === 'object' && info !== null && info.type === 'builder';
  }, [systemStatus]);
  const tabs = useChatTabs({
    load: async () => readSavedTabs(),
    save: saveTabs,
    readStatus: readTabStatus,
    newSessionId: generateSessionId,
  });
  const { ready, start, make, state, front, pins, badges, statusLoaded, notice, setNotice } = tabs;

  const agents = useMemo(() => pickableAgents(systemStatus), [systemStatus]);
  const nameOf = useCallback((agentId: string) => {
    const info = systemStatus?.agents?.[agentId];
    if (typeof info === 'object' && info !== null) return info.name;
    // Known only once the agents are read; then an agent that is not among them was deleted (F1129).
    return systemStatus && agentId && info === undefined ? GONE_AGENT : agentId;
  }, [systemStatus]);

  // Read the tabs this browser kept. The page before tabs kept one conversation in two keys: it
  // becomes the first tab, once. With nothing at all, one new tab with the workspace's agent.
  useEffect(() => {
    let live = true;
    (async () => {
      const status: SystemStatus | null = await fetch('/api/status').then(r => (r.ok ? r.json() : null)).catch(() => null);
      if (!live) return;
      setSystemStatus(status);
      let oldSession: string | null = null;
      let oldAgent: string | null = null;
      try {
        oldSession = localStorage.getItem('synapseSessionId');
        // The workspace's last agent seeds a new page only when it is a plain one: a Synapse AI tab coming forward
        // makes it the active agent too, and the next browser opened Chat as Synapse AI (F1108).
        const active = status?.active_agent_id;
        const info = active ? status?.agents?.[active] : null;
        const assistant = typeof info === 'object' && info !== null && info.type === 'builder';
        oldAgent = localStorage.getItem('synapseAgentId') || (assistant ? null : active) || null;
        localStorage.removeItem('synapseSessionId');
        localStorage.removeItem('synapseAgentId');
      } catch { /* storage blocked: nothing was kept */ }
      const asked = new URLSearchParams(window.location.search);
      const session = asked.get('session');
      const agent = asked.get('agent');
      if (session && agent) link.current = { sessionId: session, agentId: agent };
      suggestion.current = readAssistantDraft(window.location.search);
      // Chat always has a tab: with no active agent the first one there is, and with none at all a tab with no
      // agent yet — its composer, and a kept conversation reopened, as the page before tabs did (stack 1's
      // baseline found the empty strip).
      const firstAgent = oldAgent || pickableAgents(status).find(a => !a.assistant)?.id || '';
      await start(kept => {
        let next = kept;
        if (next.tabs.length === 0 && oldSession) next = openTab(next, make(oldSession, firstAgent)).state;
        if (next.tabs.length === 0) next = openTab(next, make(generateSessionId(), firstAgent, '', true)).state;
        return next;
      });
    })();
    return () => { live = false; };
  }, [start, make]);

  // The link, once the tabs are read — and, at the cap, once it is known which tabs are busy.
  useEffect(() => {
    const asked = link.current;
    if (!ready || !asked) return;
    if (state.tabs.length >= MAX_TABS && !statusLoaded) return;
    link.current = null;
    tabs.openConversation(asked.sessionId, asked.agentId);
    router.replace('/', { scroll: false });
  }, [ready, state.tabs.length, statusLoaded, tabs, router]);

  // The new Synapse AI chat a link asked for, once the tabs are read: its words in the composer, unsent, said to be a
  // suggestion; the address loses the parameters, so a reload does not open another. With no Synapse AI the words
  // still land (F1095, `draftHome`), and the link is let go of only once they have.
  useEffect(() => {
    const asked = suggestion.current;
    if (!ready || !asked) return;
    if (state.tabs.length >= MAX_TABS && !statusLoaded) return;
    const home = draftHome(agents.find(a => a.assistant)?.id, front);
    if (!home) return;
    suggestion.current = null;
    window.history.replaceState(null, '', withoutAssistantDraft(window.location.href));
    if ('open' in home) tabs.openAgent(home.open, { draft: asked.draft, suggested: true });
    else tabs.change(home.into, { draft: asked.draft, suggested: true });
  }, [ready, state.tabs.length, statusLoaded, tabs, agents, front]);

  useTabShortcuts((key) => {
    if (key.kind === 'new') setPicker(onPhone() ? 'phone' : 'strip');
    else if (key.kind === 'close') { if (state.activeId) tabs.close(state.activeId); }
    else if (state.tabs[key.index]) tabs.focus(state.tabs[key.index].id);
  }, ready);

  const stripTabs = useMemo<TabsState<ChatTab & StripTab>>(() => ({
    tabs: state.tabs.map(t => ({ ...t, tileName: nameOf(t.agentId), tileSeed: t.agentId, tileMark: isBuilder(t.agentId) })),
    activeId: state.activeId,
  }), [state, nameOf, isBuilder]);

  const frontId = front?.id;
  const changeFront = useCallback((edit: Partial<ChatTab>) => {
    if (frontId) tabs.change(frontId, edit);
  }, [frontId, tabs]);

  // The browser an agent opened, per tab (BRW2, F1073). Kept here, not in the conversation: a tab going behind
  // unmounts its conversation, and its pane comes back with it. So does the last frame of each browser.
  const [browsers, setBrowsers] = useState<Record<string, TabBrowser>>({});
  const [frames] = useState<FrameMemory>(() => new Map());
  const phone = useIsPhone();
  // A phone shows the chat or the browser, never both squeezed side by side. The browser is shown for the tab it
  // was asked for; another tab opens on its chat.
  const [phoneShows, setPhoneShows] = useState<{ tab: string; view: 'chat' | 'browser' } | null>(null);
  const phoneView = phoneShows && phoneShows.tab === frontId ? phoneShows.view : 'chat';
  const setPhoneView = (view: 'chat' | 'browser') => { if (frontId) setPhoneShows({ tab: frontId, view }); };
  const [keptShare, keepShare] = usePersisted<string>('browser-pane-share', parseShare, String(PANE_SHARE.start));
  const [dragShare, setDragShare] = useState<number | null>(null);
  const onBrowser = useCallback((tabId: string, event: BrowserEvent, agentName: string) => {
    setBrowsers(all => {
      const next = foldBrowser(all[tabId], event, agentName);
      return next === all[tabId] || !next ? all : { ...all, [tabId]: next };
    });
  }, []);
  const frontBrowser = frontId ? browsers[frontId] : undefined;
  // What the front conversation does when the person hands its browser back in the pane: its waiting turn goes on.
  const resumers = useRef<Record<string, () => void>>({});
  const showBrowser = useCallback((tabId: string, sessionId: string, agentName: string) => {
    setBrowsers(all => {
      const had = foldBrowser(all[tabId], { type: 'browser_session', session_id: sessionId, owner: '' }, agentName)!;
      return { ...all, [tabId]: { ...had, shown: true } };
    });
    setPhoneShows({ tab: tabId, view: 'browser' });
  }, []);
  const changeBrowser = useCallback((edit: Partial<TabBrowser>) => {
    if (!frontId) return;
    setBrowsers(all => (all[frontId] ? { ...all, [frontId]: { ...all[frontId], ...edit } } : all));
  }, [frontId]);
  const phoneBrowser = phone && !!frontBrowser && phoneView === 'browser';
  const pane = (props: { onHide: () => void; className?: string; style?: React.CSSProperties; children?: React.ReactNode }) => {
    if (!frontBrowser || !frontId) return null;
    const sid = frontBrowser.sessionId;
    return (
      <BrowserPane
        key={`${frontId}:${sid}`}
        host={OSS_BROWSER_HOST}
        sessionId={sid}
        agentName={frontBrowser.agentName}
        actions={frontBrowser.actions}
        memory={frames}
        closedBefore={frontBrowser.closed}
        onClosed={() => changeBrowser({ closed: true })}
        onHandedBack={() => resumers.current[frontId]?.()}
        {...props}
      />
    );
  };

  return (
    <div className="flex h-full min-h-0 overflow-hidden">
      <div className="relative flex min-w-0 flex-1 flex-col">
        {/* The strip where the page's title was: the tabs are the page's title now. */}
        <div data-tour="chat-tabs" className="hidden shrink-0 items-end gap-2 border-b border-border px-4 pt-3 md:flex md:px-6">
          {ready && (
            <TabStrip
              label="Open conversations"
              state={stripTabs}
              badges={badges}
              onFocus={tabs.focus}
              onClose={tabs.close}
              onMove={tabs.move}
              untitled="New chat"
              newTab={(
                <NewTabMenu
                  agents={agents}
                  onPick={tabs.openAgent}
                  open={picker === 'strip'}
                  onOpenChange={o => setPicker(o ? 'strip' : null)}
                  tour={{ button: 'chat-new-tab', list: 'chat-agent' }}
                />
              )}
            />
          )}
          {frontBrowser && !frontBrowser.shown && (
            <Button variant="ghost" size="sm" className="mb-1.5" onClick={() => changeBrowser({ shown: true })}>
              <Globe className="size-3.5" aria-hidden />
              Show the browser
            </Button>
          )}
          <div className="mb-1.5 shrink-0"><PageHeaderSlot /></div>
        </div>
        {/* A phone: one switcher where the strip would be (F927). */}
        <div className="flex shrink-0 items-center gap-1 border-b border-border px-3 py-2.5 md:hidden">
          {ready && (
            <TabSwitcher
              state={stripTabs}
              badges={badges}
              onFocus={tabs.focus}
              onClose={tabs.close}
              onNew={() => setPicker('phone')}
              untitled="New chat"
              subtitle={t => nameOf(t.agentId)}
              tour="chat-tab-switcher"
              className="min-w-0 flex-1"
            />
          )}
          <NewTabMenu
            agents={agents}
            onPick={tabs.openAgent}
            open={picker === 'phone'}
            onOpenChange={o => setPicker(o ? 'phone' : null)}
            // The tour's "new tab" stop on a phone, where the strip's "+" is hidden (GUIDE1).
            tour={{ button: 'chat-new-tab', list: 'chat-agent' }}
            className="size-11"
          />
          {phone && frontBrowser && (
            <Button variant="secondary" size="sm" onClick={() => setPhoneView(phoneView === 'browser' ? 'chat' : 'browser')}>
              {phoneView === 'browser'
                ? <><MessageSquare className="size-3.5" aria-hidden />Chat</>
                : <><Globe className="size-3.5" aria-hidden />Browser</>}
            </Button>
          )}
          <IconButton icon={History} label="History" onClick={() => setPanel('history')} />
        </div>

        {/* Running / paused orchestrations — click through to the live run view */}
        <ActiveRunsBanner />

        {notice && (
          <div role="status" className="mx-auto mt-3 flex w-full max-w-3xl items-center gap-3 rounded-lg border border-border bg-surface px-3 py-2.5 text-xs text-text-muted">
            <span className="min-w-0 flex-1">{notice}</span>
            <IconButton icon={X} label="Dismiss" size="sm" onClick={() => setNotice(null)} />
          </div>
        )}

        {phoneBrowser && pane({ onHide: () => setPhoneView('chat'), className: 'flex-1' })}

        {front && (
          // Hidden, never unmounted, while a phone shows the browser: the turn keeps being read.
          <div className={phoneBrowser ? 'hidden' : 'contents'}>
            <ChatConversation
              key={front.id}
              tab={front}
              onChange={changeFront}
              onBusy={tabs.setFrontBusy}
              onOpenAgent={tabs.openAgent}
              onOpenChat={(sessionId, agentId, title) => tabs.openConversation(sessionId, agentId, tabTitle(title))}
              onBrowser={(event, agentName) => onBrowser(front.id, event, agentName)}
              onShowBrowser={(sessionId, agentName) => showBrowser(front.id, sessionId, agentName)}
              onResumeHook={(resume) => { resumers.current[front.id] = resume; }}
            />
          </div>
        )}
      </div>

      {!phone && frontBrowser?.shown && pane({
        onHide: () => changeBrowser({ shown: false }),
        className: 'shrink-0 border-l border-border',
        style: { width: `${(dragShare ?? Number(keptShare)) * 100}%` },
        children: (
          <PaneHandle
            share={dragShare ?? Number(keptShare)}
            onShare={setDragShare}
            onDone={(n) => { keepShare(String(n)); setDragShare(null); }}
            label="Resize the browser pane"
          />
        ),
      })}

      <SideRail
        open={panel}
        onOpen={setPanel}
        tour="chat-rail"
        className="hidden border-l border-border px-1.5 md:flex"
        descriptions={{
          history: 'Opening one puts it in a tab, or goes to its tab if it is open.',
          pinned: 'Conversations you pinned, to find again.',
          search: 'Any question or answer in your conversations.',
        }}
        render={(which) => (
          <ConversationList
            which={which}
            tabs={state.tabs}
            frontKey={front?.key ?? null}
            pins={pins}
            nameOf={nameOf}
            isAssistant={isBuilder}
            onOpen={(s) => { setPanel(null); tabs.openConversation(s.session_id, s.agent_id, tabTitle(s.first_user_message || '')); }}
            onTogglePin={tabs.togglePin}
            onNewChat={() => { setPanel(null); setPicker(onPhone() ? 'phone' : 'strip'); }}
          />
        )}
      />
    </div>
  );
}

/**
 * History, Pinned or Search, in the rail's drawer (F926). History keeps what its
 * drawer had: each conversation's agent, when, its first question and last
 * answer, its turns, Refresh, Delete, and New chat. Search asks the server, so
 * a conversation the list hasn't drawn is still found.
 */
function ConversationList({ which, tabs, frontKey, pins, nameOf, isAssistant, onOpen, onTogglePin, onNewChat }: {
  which: RailPanel;
  tabs: ChatTab[];
  frontKey: string | null;
  pins: TabPin[];
  nameOf: (agentId: string) => string;
  /** Synapse AI's chats are listed among the agents' (TABS2, F1062), with its mark. */
  isAssistant: (agentId: string) => boolean;
  onOpen: (s: SessionSummary) => void;
  onTogglePin: (pin: TabPin) => void;
  onNewChat: () => void;
}) {
  const [query, setQuery] = useState('');
  const [nonce, setNonce] = useState(0);
  const words = which === 'search' ? query.trim() : '';
  // What was read, and for which ask: the list shown is the one this ask read, or nothing yet.
  const asking = `${words}|${nonce}`;
  const [read, setRead] = useState<{ asking: string; list: SessionSummary[] } | null>(null);
  const idle = which === 'search' && !words;
  const loading = !idle && read?.asking !== asking;
  const sessions = idle ? [] : read?.list ?? [];

  useEffect(() => {
    if (idle) return;
    let live = true;
    const wait = setTimeout(() => {
      fetch(words ? `/api/sessions?q=${encodeURIComponent(words)}` : '/api/sessions')
        .then(r => (r.ok ? r.json() : []))
        .then(list => { if (live) setRead({ asking, list: Array.isArray(list) ? list : [] }); })
        .catch(e => { console.error('[Sessions] fetch error', e); if (live) setRead({ asking, list: [] }); });
    }, words ? 250 : 0);
    return () => { live = false; clearTimeout(wait); };
  }, [idle, words, asking]);

  const pinned = (s: SessionSummary) => pins.some(p => p.sessionId === s.session_id && p.agentId === s.agent_id);
  const openKeys = new Set(tabs.map(t => t.key));
  const shown = which === 'pinned'
    ? pins.map(p => sessions.find(s => s.session_id === p.sessionId && s.agent_id === p.agentId)).filter((s): s is SessionSummary => !!s)
    : sessions;

  const remove = async (s: SessionSummary) => {
    await fetch(`/api/sessions/${s.session_id}?agent_id=${s.agent_id}`, { method: 'DELETE' });
    setRead(prev => prev && { ...prev, list: prev.list.filter(x => !(x.session_id === s.session_id && x.agent_id === s.agent_id)) });
    if (pinned(s)) onTogglePin({ sessionId: s.session_id, agentId: s.agent_id });
  };

  return (
    <div className="-mx-5 -my-4 flex min-h-full flex-col">
      {which === 'search' && (
        <div className="border-b border-border px-4 py-3">
          <input
            type="search"
            value={query}
            onChange={e => setQuery(e.target.value)}
            aria-label="Search your conversations"
            placeholder="Search your conversations"
            autoFocus
            className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text placeholder:text-text-faint"
          />
        </div>
      )}
      {which === 'history' && (
        <div className="flex justify-end border-b border-border px-3 py-1.5">
          <button
            type="button"
            onClick={() => setNonce(n => n + 1)}
            disabled={loading}
            className="rounded-md p-1.5 text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
            title="Refresh sessions"
            aria-label="Refresh sessions"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          </button>
        </div>
      )}
      <div className="flex-1">
        {loading && shown.length === 0 && (
          <div className="flex items-center justify-center py-10 text-xs text-text-faint">Loading...</div>
        )}
        {!loading && shown.length === 0 && (
          <div className="flex flex-col items-center justify-center gap-2 py-10">
            <Clock className="h-8 w-8 text-text-faint" />
            <span className="text-xs text-text-faint">
              {which === 'pinned' ? 'Nothing pinned yet' : which === 'search' ? (words ? 'Nothing matches' : 'Type to search') : 'No chat history yet'}
            </span>
          </div>
        )}
        {shown.map(s => {
          const key = chatKey(s.session_id, s.agent_id);
          const isFront = key === frontKey;
          const isPinned = pinned(s);
          return (
            // The row is a button, so a keyboard opens a conversation as a click does; Pin and
            // Delete are its siblings, named and shown on focus (F236).
            <div
              key={`${s.agent_id}_${s.session_id}`}
              className={cn("group relative border-b border-border transition-colors hover:bg-row-hover", isFront && "bg-surface-2")}
            >
              {isFront && <div className="absolute bottom-0 left-0 top-0 w-0.5 bg-accent" />}
              <button
                type="button"
                onClick={() => onOpen(s)}
                aria-current={isFront ? 'true' : undefined}
                className="block w-full px-4 py-3 pr-16 text-left"
              >
                <span className="mb-1 flex items-start justify-between gap-2">
                  {/* The agent's name, not its id in capitals (U59); "Deleted agent" for a deleted one (F1129); the id as the tooltip. */}
                  <span className="flex min-w-0 items-center gap-1.5 truncate text-xs font-medium text-text-faint" title={s.agent_id || 'default'}>
                    {isAssistant(s.agent_id) && <Mark size={12} compact={false} className="shrink-0 text-accent" aria-hidden />}
                    {nameOf(s.agent_id) || s.agent_id || 'default'}
                    {openKeys.has(key) && !isFront && <span className="font-normal"> · open in a tab</span>}
                  </span>
                  <span className="shrink-0 text-xs text-text-faint">{formatRelativeTime(s.last_updated)}</span>
                </span>
                <span className="block line-clamp-2 text-[13px] leading-5 text-text">
                  {s.first_user_message || '(no messages)'}
                </span>
                {s.last_response && (
                  <span className="mt-1 block truncate text-xs text-text-faint">
                    ↩ {s.last_response.slice(0, 80)}{s.last_response.length > 80 ? '…' : ''}
                  </span>
                )}
                <span className="mt-1 block text-xs text-text-faint">
                  {s.turn_count} turn{s.turn_count !== 1 ? 's' : ''}
                </span>
              </button>
              <div className="absolute right-3 top-2.5 flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => onTogglePin({ sessionId: s.session_id, agentId: s.agent_id })}
                  aria-pressed={isPinned}
                  className={cn(
                    "rounded-md p-0.5 transition-all hover:text-text group-focus-within:opacity-100 group-hover:opacity-100",
                    isPinned ? "text-accent-ink opacity-100" : "text-text-faint opacity-0",
                  )}
                  aria-label={`${isPinned ? 'Unpin' : 'Pin'} the conversation "${(s.first_user_message || 'no messages').slice(0, 40)}"`}
                  title={isPinned ? 'Unpin' : 'Pin'}
                >
                  <Pin className="h-3 w-3" aria-hidden />
                </button>
                <button
                  type="button"
                  onClick={() => { void remove(s); }}
                  className="rounded-md p-0.5 text-text-faint opacity-0 transition-all hover:text-danger group-focus-within:opacity-100 group-hover:opacity-100"
                  aria-label={`Delete the conversation "${(s.first_user_message || 'no messages').slice(0, 40)}"`}
                  title="Delete session"
                >
                  <Trash2 className="h-3 w-3" aria-hidden />
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {which === 'history' && (
        <div className="sticky bottom-0 shrink-0 border-t border-border bg-surface px-4 py-3">
          <Button type="button" variant="secondary" size="sm" onClick={onNewChat} className="w-full">
            <Plus className="h-3.5 w-3.5" aria-hidden />
            New Chat
          </Button>
        </div>
      )}
    </div>
  );
}
