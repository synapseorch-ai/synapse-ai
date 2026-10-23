/*
 * The AI builder's plan, read out of the question it asks (M2c).
 *
 * The builder asks for approval with a human step whose prompt is a fixed
 * lead-in, a `---` rule and the plan as markdown (`native_builder/
 * orchestration.json`: `step_human_review` for a new build, `step_manage_review`
 * for a change). Nothing structured comes with it, so this reads the markdown:
 * the steps, who does each one, the schedule, the agents it would create and
 * any open questions. The model writes the steps in a few shapes — a numbered
 * list of `**Name** (type)`, `### N. Name (type)` headings, a table with Name and
 * Type columns, a `Type:` line under the name — and every one the dev stack and
 * the April–May samples hold reads here. When nothing reads, `readPlan` says so
 * with `null` and the card shows the text as it was asked.
 *
 * Pure: no React, no request. Synced into the cloud app with the rest of
 * `components/assistant/`.
 */
import { roleOf, type StepRole } from '@/components/run/model';
import { STEP_TYPE_META, type StepType } from '@/types/orchestration';

/** Which of the builder's questions a prompt is. */
export type QuestionKind = 'build' | 'change' | 'clarify' | 'other';

/** The builder's human steps, fixed in its workflow. */
export const BUILDER_QUESTION_STEPS = {
    build: 'step_human_review',
    change: 'step_manage_review',
    clarify: 'step_human_clarify',
} as const;

const BUILD_LEAD = /^\s*Here is the proposed plan\b/i;
const CHANGE_LEAD = /^\s*Here is what I would do\b/i;

/**
 * By the step that asked (live events and a reload both have it), else by the
 * template's lead-in — an answered question replayed from the journal may carry
 * only its words.
 */
export function questionKind(stepId: string | null | undefined, prompt: string): QuestionKind {
    if (stepId === BUILDER_QUESTION_STEPS.build) return 'build';
    if (stepId === BUILDER_QUESTION_STEPS.change) return 'change';
    if (stepId === BUILDER_QUESTION_STEPS.clarify) return 'clarify';
    if (BUILD_LEAD.test(prompt)) return 'build';
    if (CHANGE_LEAD.test(prompt)) return 'change';
    return 'other';
}

/** The lead-in ("Reply approve…") and the plan under the template's rule. */
export function splitQuestion(prompt: string): { lead: string; body: string } {
    const match = /\n[ \t]*-{3,}[ \t]*\n/.exec(prompt);
    if (!match) return { lead: '', body: prompt.trim() };
    return { lead: prompt.slice(0, match.index).trim(), body: prompt.slice(match.index + match[0].length).trim() };
}

export interface PlanStep {
    n: number;
    name: string;
    /** A step type the engine knows, or null when the line named none. */
    type: StepType | null;
    /** What the card prints on the right: the type's own label. */
    label: string;
    role: StepRole;
    /** A change plan's line: what the action does. */
    detail?: string;
    /** A change plan's `⚠️ DELETES:` line. */
    deletes?: boolean;
}

export interface Plan {
    kind: 'build' | 'change';
    /** The plan as asked, under the lead-in — what "Show all of it" opens. */
    body: string;
    overview: string | null;
    steps: PlanStep[];
    schedule: string | null;
    newAgents: string[];
    openQuestions: string | null;
    /**
     * What pasted Agents SDK code held that the plan could not build — a
     * credential, an MCP server, a function tool — and what to do about each
     * (SW1). Only a plan drafted from pasted code has the section.
     */
    notCarried: string | null;
    /**
     * What the plan names that nothing in the workspace provides, said by the
     * builder's own check after its one redraft (F492, BWA1): approved as it
     * is, the build would stop there.
     */
    needs: string | null;
    /** What the plan's tools change outside, and who approves each (BWA1, decision 4). */
    asksFirst: string | null;
    /** What the person said may act without anyone approving it each time. */
    actsAlone: string | null;
    /**
     * The answers the card offers, when it is drawn from the plan's object (SAI2-P3): `build`, `without:<tool>`,
     * `find`, `change`, `stop`. Absent for a plan read from its words, which offers what it always did.
     */
    offers?: string[];
    /** Each new agent's instructions, as the person approves them (SAI2-P3, Q2): drawn only from the plan's object. */
    agentInstructions?: { name: string; instructions: string }[];
    /** What one run may spend at most (CAP1), in words: drawn only from the plan's object. */
    limits?: string | null;
}

// ── Step types ───────────────────────────────────────────────────────────────

const TYPE_KEYS = Object.keys(STEP_TYPE_META) as StepType[];

/** Each type as it may be written: `extract_json`, "extract json", `if/else`… */
const TYPE_PATTERNS: [StepType, RegExp][] = TYPE_KEYS.map((key) => [
    key,
    new RegExp(`(?:^|[^a-z_])(${key.split('_').join('[\\s_/-]*')})(?![a-z_])`, 'i'),
]);

/** Loose words the model uses for a type, beside its own name. */
const LOOSE: [StepType, RegExp][] = [
    ['llm', /(?:^|[^a-z])llm[\s-]*call\b/i],
    ['human', /(?:^|[^a-z])(?:human[\s-]*(?:checkpoint|review|approval)|approval)\b/i],
];

/** The first step type a fragment names, by where it appears in the text. */
export function typeIn(fragment: string): StepType | null {
    const text = fragment.replace(/[`*_]+(?=[a-z])|(?<=[a-z])[`*]+/gi, ' ');
    let best: { type: StepType; at: number } | null = null;
    for (const [type, pattern] of [...TYPE_PATTERNS, ...LOOSE]) {
        const m = pattern.exec(text);
        if (!m) continue;
        const at = m.index;
        if (!best || at < best.at) best = { type, at };
    }
    return best?.type ?? null;
}

function stepOf(n: number, name: string, type: StepType | null): PlanStep {
    return {
        n,
        name,
        type,
        label: type ? STEP_TYPE_META[type].label : 'Step',
        role: roleOf(type),
    };
}

/** Markdown emphasis and code marks off a name, and a trailing colon. */
function clean(text: string): string {
    return text
        .replace(/\*\*|__|`/g, '')
        .replace(/^\*|\*$/g, '')
        .replace(/\s*:\s*$/, '')
        .replace(/\s+/g, ' ')
        .trim();
}

// ── Sections ─────────────────────────────────────────────────────────────────

interface Section { title: string; level: number; text: string }

/** Split at `##`/`###` headings. Text before the first heading is the preamble. */
function sections(body: string): { preamble: string; list: Section[] } {
    const lines = body.split('\n');
    const list: Section[] = [];
    const preamble: string[] = [];
    let current: Section | null = null;
    let fence = false;
    for (const line of lines) {
        if (/^\s*```/.test(line)) fence = !fence;
        const heading = fence ? null : /^(#{1,3})\s+(.+?)\s*#*\s*$/.exec(line);
        // A numbered heading (`### 2. Name (type)`) is a step, not a section.
        if (heading && !/^\d+[.)]\s/.test(heading[2])) {
            current = { title: clean(heading[2]).toLowerCase(), level: heading[1].length, text: '' };
            list.push(current);
            continue;
        }
        if (current) current.text += `${line}\n`;
        else preamble.push(line);
    }
    return { preamble: preamble.join('\n').trim(), list };
}

function find(list: Section[], test: RegExp): Section | undefined {
    return list.find((s) => test.test(s.title));
}

/** A section and the subsections under it (a `###` inside a `##`). */
function withChildren(list: Section[], section: Section | undefined): string | null {
    const at = section ? list.indexOf(section) : -1;
    if (at < 0) return null;
    let text = list[at].text;
    for (let i = at + 1; i < list.length && list[i].level > list[at].level; i++) {
        text += `### ${list[i].title}\n${list[i].text}`;
    }
    return text;
}

// ── The build plan's steps ───────────────────────────────────────────────────

function tableSteps(text: string): PlanStep[] {
    const rows = text.split('\n').filter((l) => /^\s*\|/.test(l));
    if (rows.length < 3) return [];
    const cells = (row: string) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    const head = cells(rows[0]).map((c) => clean(c).toLowerCase());
    const nameAt = head.findIndex((c) => /^(name|step)\b/.test(c));
    const typeAt = head.findIndex((c) => /^type\b/.test(c));
    if (nameAt < 0) return [];
    const steps: PlanStep[] = [];
    for (const row of rows.slice(1)) {
        const c = cells(row);
        if (c.every((x) => /^:?-{2,}:?$/.test(x) || !x)) continue; // the separator row
        const name = clean(c[nameAt] ?? '');
        if (!name) continue;
        const type = typeAt >= 0 ? typeIn(c[typeAt] ?? '') : typeIn(row);
        steps.push(stepOf(steps.length + 1, name, type));
    }
    return steps;
}

/** `1. **Name** (type)`, `### 2. Name *(type)*`, `3. **Name**` with `- Type: llm` under it. */
function listSteps(text: string): PlanStep[] {
    const lines = text.split('\n');
    const steps: PlanStep[] = [];
    let open: { name: string; type: StepType | null } | null = null;
    const close = () => {
        if (open) steps.push(stepOf(steps.length + 1, open.name, open.type));
        open = null;
    };
    for (const line of lines) {
        const item = /^\s{0,3}(?:#{2,4}\s*)?\d+[.)]\s+(.+)$/.exec(line);
        if (item) {
            close();
            const rest = item[1];
            const bold = /^\*\*(.+?)\*\*(.*)$/.exec(rest);
            let name: string;
            let after: string;
            if (bold) {
                name = bold[1];
                after = bold[2];
            } else {
                const paren = rest.search(/\s[*_]?\(/);
                const dash = rest.search(/\s[–—-]\s/);
                const cut = [paren, dash].filter((i) => i > 0).sort((a, b) => a - b)[0];
                name = cut ? rest.slice(0, cut) : rest;
                after = cut ? rest.slice(cut) : '';
            }
            const inParens = /\(([^)]*)\)/.exec(after);
            const type = typeIn(inParens ? inParens[1] : after);
            open = { name: clean(name), type };
            continue;
        }
        // A sub-bullet naming the type, for a step whose own line did not.
        const current = open as { name: string; type: StepType | null } | null;
        if (current && !current.type) {
            const typeLine = /^\s+[-*]\s+(?:\*\*)?(?:step\s+)?type(?:\*\*)?\s*:?\s*(?:\*\*)?\s*:?\s*(.+)$/i.exec(line);
            if (typeLine) current.type = typeIn(typeLine[1]);
        }
    }
    close();
    return steps.filter((s) => s.name);
}

// ── The change plan's actions ────────────────────────────────────────────────

function changeSteps(text: string): PlanStep[] {
    const steps: PlanStep[] = [];
    for (const line of text.split('\n')) {
        const item = /^\s{0,3}(?:[-*•]|\d+[.)])\s+(.+)$/.exec(line);
        if (!item) continue;
        const raw = item[1].trim();
        const deletes = /DELETES\s*:/i.test(raw);
        const words = raw.replace(/^(?:⚠️|⚠)\s*/u, '').replace(/^\*{0,2}DELETES\s*:\s*\*{0,2}\s*/i, '');
        const split = /^(.+?)\s+[—–-]\s+(.+)$/.exec(words);
        const name = clean(split ? split[1] : words);
        if (!name) continue;
        steps.push({
            n: steps.length + 1,
            name: deletes ? `Deletes: ${name}` : name,
            type: null,
            label: '',
            role: 'logic',
            ...(split ? { detail: clean(split[2]) } : {}),
            ...(deletes ? { deletes: true } : {}),
        });
    }
    return steps;
}

// ── Pieces around the steps ──────────────────────────────────────────────────

function firstParagraph(text: string | undefined | null): string | null {
    if (!text) return null;
    const para = text.trim().split(/\n\s*\n/)[0]?.trim();
    return para || null;
}

function isNone(text: string): boolean {
    return /^[\s\-*_(]*(?:none|n\/a|no open questions)\b/i.test(text.replace(/[*_]/g, '').trim());
}

function scheduleOf(text: string | undefined): string | null {
    if (!text) return null;
    const when = /\*{0,2}when\*{0,2}\s*:?\s*\*{0,2}\s*:?\s*(.+)/i.exec(text);
    const line = when ? when[1] : text.trim().split('\n').find((l) => l.trim())?.replace(/^\s*[-*]\s*/, '');
    if (!line) return null;
    const words = clean(line.replace(/`[^`]*`/g, '').replace(/\(\s*\)/g, '')).replace(/\.\s*$/, '').trim();
    return words && !isNone(words) ? words : null;
}

function newAgentsOf(text: string | null): string[] {
    if (!text) return [];
    const from = text.search(/new agents?(?: to create)?/i);
    if (from < 0) return [];
    // From the line after "New agents to create" to the next part of the summary.
    let region = text.slice(from).replace(/^[^\n]*\n?/, '');
    const stop = region.search(/existing agents?|^#{1,2}\s/im);
    if (stop >= 0) region = region.slice(0, stop);
    const names: string[] = [];
    const add = (raw: string) => {
        const name = clean(raw).replace(/\s*\(.*\)$/, '');
        if (name && !isNone(name) && !names.includes(name)) names.push(name);
    };
    // Each agent's own `name:` line, in whichever markup the model chose…
    for (const m of region.matchAll(/^\s*[-*]?\s*`?\**name\**\s*:?\**\s*:?\s*`?([^`\n]+?)`?\s*$/gim)) add(m[1]);
    if (names.length) return names;
    // …else the bold name that opens each bullet.
    for (const m of region.matchAll(/^\s{0,1}[-*]\s+\*\*([^*\n]+?)\*\*/gm)) {
        if (!/^(role|name|type|purpose|tools|repos|db_configs|delegate_agent_ids)\s*:?$/i.test(m[1].trim())) add(m[1]);
    }
    return names;
}

// ── The plan ─────────────────────────────────────────────────────────────────

/**
 * A build plan's steps as a workflow to price (CE1): each step it names a type
 * for, once, one after another, on the workspace's default model. A plan names
 * no branch and no model, so this is every step it lists: the most a run of it
 * costs, unless it loops. `null` for a change plan (its lines are actions) or a
 * plan with no typed step.
 */
export function planDefinition(plan: Plan): { entry_step_id: string; steps: Record<string, unknown>[] } | null {
    if (plan.kind !== 'build') return null;
    const typed = plan.steps.filter((s) => s.type !== null && s.type !== 'end');
    if (!typed.length) return null;
    const steps = typed.map((s, i) => ({
        id: `plan_${i + 1}`,
        name: s.name,
        type: s.type,
        next_step_id: i + 1 < typed.length ? `plan_${i + 2}` : null,
    }));
    return { entry_step_id: steps[0].id, steps };
}

// ── A change, from its own object (SAI2-P2, F798) ─────────────────────────────

/** One op of Synapse AI's checked change, as its review's event carries it (`check_change`). */
export interface ChangeOpView {
    op: string;
    kind: 'act' | 'write' | 'delete' | 'create' | 'handoff';
    status: string;
    target?: { kind?: string; id?: string | null; name?: string | null };
    /** Code's words for it: "<what> — <how>". */
    line?: string;
    /** For a delete: what uses its target, by name. */
    used_by?: string[];
}

/** The checked change a review carries when its object is one (`context.object === 'change'`). */
export interface ChangeView {
    object: 'change';
    ops: ChangeOpView[];
    /** What else the check said: the cards it put up, what was already so, what the role may not do. */
    say?: string;
}

export function changeOf(context: unknown): ChangeView | null {
    if (!context || typeof context !== 'object') return null;
    const c = context as { object?: unknown; ops?: unknown };
    return c.object === 'change' && Array.isArray(c.ops) ? (context as ChangeView) : null;
}

/**
 * The change's card, from the object rather than its markdown: one row per op
 * the person approves, a delete in the danger colour with what uses it. Null
 * when nothing waits for approval; the card then shows the words as asked.
 */
export function planFromChange(change: ChangeView, prompt: string): Plan | null {
    const ready = change.ops.filter((o) => o.status === 'ready' && o.line);
    if (!ready.length) return null;
    // Two targets of one name, not two ops on one target, are told apart by id.
    const count = (name: string) =>
        new Set(ready.filter((o) => (o.target?.name ?? '') === name).map((o) => o.target?.id ?? '')).size;
    const steps: PlanStep[] = ready.map((o, i) => {
        const [what, ...how] = (o.line as string).split(' — ');
        const name = o.target?.name ?? '';
        // Two of one name are told apart by id (F760's class).
        const id = count(name) > 1 && o.target?.id ? ` (${o.target.id})` : '';
        const deletes = o.kind === 'delete';
        const uses = deletes
            ? `${o.used_by?.length ? `Used by ${o.used_by.join(', ')}.` : 'Nothing uses it.'} It can come back only as a new copy.`
            : '';
        const detail = [how.join(' — '), uses].filter(Boolean).join(' ');
        return {
            n: i + 1,
            name: deletes ? `Deletes: the ${o.target?.kind ?? ''} "${name}"${id}` : `${what}${id}`,
            type: null,
            label: '',
            role: 'logic',
            ...(detail ? { detail } : {}),
            ...(deletes ? { deletes: true } : {}),
        };
    });
    return {
        kind: 'change', body: splitQuestion(prompt).body, overview: change.say?.trim() || null, steps,
        schedule: null, newAgents: [], openQuestions: null, notCarried: null, needs: null, asksFirst: null, actsAlone: null,
    };
}

// ── A build, from its own object (SAI2-P3, F939) ──────────────────────────────

/** One step of Synapse AI's checked plan, as the review's event carries it (`check_build`). */
export interface PlanObjectStep {
    id: string;
    type: string;
    name: string;
    does?: string;
    tool?: string | null;
    agent?: string | null;
    workflow?: string | null;
}

/** One approval the run asks for, or one block: who, before which tool, and what set it (F964). */
export interface PlanGate {
    who: string;
    tool: string;
    decision: 'confirm' | 'deny';
    set_by: 'build' | 'agent' | 'workspace';
    kind?: 'agent' | 'step';
}

/** A gate as the card says it: the words `render.gate_words` writes on the review. */
export function gateWords(gate: PlanGate): string {
    const who = gate.kind === 'step' ? `The step "${gate.who}"` : gate.who;
    if (gate.decision === 'deny') {
        const whose = gate.set_by === 'agent' ? 'its own tool policy' : "this workspace's tool policy";
        return `${who} can't use \`${gate.tool}\`: ${whose} blocks it`;
    }
    const by = gate.set_by === 'agent' ? ' (its own tool policy)'
        : gate.set_by === 'workspace' ? " (this workspace's tool policy)" : '';
    return `${who} asks you before \`${gate.tool}\`, each time${by}`;
}

/** What asks first, by who asks (F964), then a tool a Human step approves, then what is blocked. */
function asksOf(view: PlanObjectView): string | null {
    const gates = view.plan.gates ?? [];
    const confirms = gates.filter((g) => g.decision === 'confirm');
    const byGate = new Set(confirms.map((g) => g.tool));
    const lines = [
        ...confirms.map(gateWords),
        ...(view.plan.approvals?.asks_first ?? []).filter((t) => !byGate.has(t)).map((t) => `\`${t}\``),
        ...gates.filter((g) => g.decision === 'deny').map(gateWords),
    ];
    if (!lines.length) return null;
    const human = view.plan.steps.some((s) => s.type === 'human');
    return lines.map((l) => `- ${l}`).join('\n')
        + (confirms.length && human ? '\n\nThese are asked as well as the Human steps above.' : '');
}

/** The checked plan a build's review carries when its object is one (`context.object === 'plan'`). */
export interface PlanObjectView {
    object: 'plan';
    outcome: string;
    plan: {
        kind: 'create' | 'update';
        title?: string;
        summary?: string;
        steps: PlanObjectStep[];
        agents?: { name: string; type?: string }[];
        schedule?: { cron?: string; says?: string; timezone?: string | null; input?: string | null } | null;
        caps?: { credits?: number | null; dollars?: number | null } | null;
        questions?: string[];
        not_carried_over?: string[];
        approvals?: { asks_first?: string[]; acts_without_asking?: string[] };
        /** Every approval the run asks for and every block, as the engine's policy decides them (F964). */
        gates?: PlanGate[];
        tools?: { name: string; status: string; provider?: string | null; via?: string }[];
    };
    /** What the card offers (decision 2, Naveen 10-23): no `build` while a tool has no way in. */
    offers: string[];
    /** Each new agent's instructions, written before the review and covered by the yes (Naveen 10-23, Q2). */
    prompts?: Record<string, string>;
    /** An update's changes against the workflow as it is, in code's words: what the card lists as its rows. */
    changes?: string[];
    /** Code's words: each missing tool and its reason, and what the plan could not be saved for. */
    say?: string;
}

export function planOf(context: unknown): PlanObjectView | null {
    if (!context || typeof context !== 'object') return null;
    const c = context as { object?: unknown; outcome?: unknown; plan?: { steps?: unknown }; offers?: unknown };
    return c.object === 'plan' && c.outcome === 'review' && Array.isArray(c.plan?.steps) && Array.isArray(c.offers)
        ? (context as PlanObjectView) : null;
}

function bullets(items: string[] | undefined, code = false): string | null {
    const kept = (items ?? []).map((i) => i.trim()).filter(Boolean);
    return kept.length ? kept.map((i) => (code ? `- \`${i}\`` : `- ${i}`)).join('\n') : null;
}

/**
 * The build's card, from the object rather than its markdown: its steps by type, what it makes, when it runs, what
 * asks first, what is missing and why, the open questions, and the answers it offers. A field with nothing in it is
 * never drawn (no "(cron expression: )"). The words stay the fallback for an older run (F443).
 */
export function planFromPlanObject(view: PlanObjectView, prompt: string): Plan | null {
    const steps = view.plan.steps.filter((s) => s.type !== 'end');
    if (!steps.length) return null;
    // An update is read as what it changes, not the whole workflow again (SAI2's review, 13).
    const changes = (view.changes ?? []).filter((c) => c.trim());
    const known = (t: string): StepType | null => (t in STEP_TYPE_META ? (t as StepType) : null);
    const when = view.plan.schedule?.says?.trim() || view.plan.schedule?.cron?.trim();
    const cron = view.plan.schedule?.says?.trim() ? view.plan.schedule?.cron?.trim() : '';
    return {
        kind: 'build',
        body: splitQuestion(prompt).body,
        overview: view.plan.summary?.trim() || null,
        steps: changes.length
            ? changes.map((c, i) => ({ n: i + 1, name: c, type: null, label: '', role: 'logic' as const }))
            : steps.map((s, i) => {
                const step = stepOf(i + 1, s.name, known(s.type));
                const detail = [s.does && s.does.trim() !== s.name.trim() ? s.does.trim() : '', s.tool ? `uses ${s.tool}` : '']
                    .filter(Boolean).join(' — ');
                return detail ? { ...step, detail } : step;
            }),
        schedule: when
            ? [cron ? `${when} (${cron}, UTC)` : when, view.plan.schedule?.input?.trim()
                ? `each run starting with: ${view.plan.schedule.input.trim()}` : ''].filter(Boolean).join(', ')
            : null,
        limits: [
            view.plan.caps?.credits != null ? `${view.plan.caps.credits} credits` : '',
            view.plan.caps?.dollars != null ? `$${view.plan.caps.dollars} of models` : '',
        ].filter(Boolean).join(' and ') || null,
        newAgents: (view.plan.agents ?? []).map((a) => a.name).filter(Boolean),
        openQuestions: bullets(view.plan.questions),
        notCarried: bullets(view.plan.not_carried_over),
        needs: view.say?.trim() || null,
        asksFirst: asksOf(view),
        actsAlone: bullets(view.plan.approvals?.acts_without_asking, true),
        offers: view.offers,
        agentInstructions: Object.entries(view.prompts ?? {})
            .filter(([, text]) => text.trim())
            .map(([name, text]) => ({ name, instructions: text.trim() })),
    };
}

/** What a "Build without" answer sends, as `read_plan_answer` reads it. */
export function withoutWords(tool: string): string {
    return `build without ${tool}`;
}

/** What "Let it act without asking" sends: the person's own move of a tool out of "asks you first" (BWA1, decision 4). */
export function aloneWords(tool: string): string {
    return `let ${tool} act without asking`;
}

/**
 * Read a plan question. `null` when no step can be read, and the card then
 * shows the words as they were asked.
 */
export function readPlan(prompt: string, kind: 'build' | 'change'): Plan | null {
    const { body } = splitQuestion(prompt);
    if (!body) return null;
    const { preamble, list } = sections(body);

    if (kind === 'change') {
        const steps = changeSteps(body);
        if (!steps.length) return null;
        const overview = firstParagraph(preamble.split('\n').filter((l) => !/^\s*(?:[-*•]|\d+[.)])\s/.test(l)).join('\n'));
        return { kind, body, overview, steps, schedule: null, newAgents: [], openQuestions: null, notCarried: null, needs: null, asksFirst: null, actsAlone: null };
    }

    const stepsSection = find(list, /^steps?\b/) ?? find(list, /\bsteps\b/);
    if (!stepsSection) return null;
    const stepText = withChildren(list, stepsSection) ?? stepsSection.text;
    let steps = tableSteps(stepText);
    if (!steps.length) steps = listSteps(stepText);
    if (!steps.length) return null;

    const open = find(list, /open questions?/)?.text.trim() ?? '';
    const left = find(list, /^not carried over\b/)?.text.trim() ?? '';
    const needs = find(list, /^missing here\b/)?.text.trim() ?? '';
    const asks = find(list, /^asks you first\b/)?.text.trim() ?? '';
    const alone = find(list, /^acts without asking\b/)?.text.trim() ?? '';
    return {
        kind,
        body,
        overview: firstParagraph(find(list, /^overview\b/)?.text ?? preamble),
        steps,
        schedule: scheduleOf(find(list, /^schedule\b/)?.text),
        newAgents: newAgentsOf(withChildren(list, find(list, /^agents?\b/))),
        openQuestions: open && !isNone(open) ? open : null,
        notCarried: left && !isNone(left) && !/^[\s\-*_]*nothing\b/i.test(left) ? left : null,
        needs: needs || null,
        asksFirst: asks && !isNone(asks) ? asks : null,
        actsAlone: alone && !isNone(alone) ? alone : null,
    };
}
