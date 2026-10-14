"use client";

/*
 * /evals — rubrics, golden sets, and what the judge thought of every run
 * measured against a rubric (M2h: the list beside the open one).
 *
 * Note the vocabulary, because the product already uses the other word: an
 * **Evaluator** is a routing step inside a workflow and has nothing to do with
 * this screen. What lives here is the **judge** — a model that scores a
 * finished run against a rubric you wrote.
 *
 * The list holds the rubrics and the golden sets; the first rubric is open, so
 * the page never opens on an empty pane (UX1). A rubric opens on its scores for
 * one of its workflows ("Scores for", cloud's model: Naveen, M2h's plan), then
 * its setup. The trend shows a break where the rubric version changed: scores
 * either side of one answer different questions, and a continuous line through
 * that point would invent a regression (or hide one). The figures, the trend
 * and the scored runs are the shared `components/run/EvalTrend`.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Play, Plus, Sparkles, Square, Target, Trash } from "lucide-react";

import { AppScreen } from "@/components/app/AppScreen";
import { DetailHead, EvalTrend, ListRow, useClock, When, type EvalTrendData } from "@/components/run";
import {
    Button, Combobox, ConfirmDialog, EmptyState, Field, IconButton, Input, Muted, Section, Select,
    Textarea, Toast, useToast,
} from "@/components/ui";
import { cn } from "@/lib/cn";
import { count } from "@/lib/copy";
import { modelOptions, withUnreachable } from "@/lib/models";

import { useSettingsData } from "./hooks/useSettingsData";

type Criterion = { id?: string; name: string; weight?: number; guidance?: string };

type Rubric = {
    id: string;
    name: string;
    description: string;
    version: number;
    criteria: Criterion[];
    scale: { min: number; max: number };
    floor: number | null;
    drop: number | null;
    baseline_runs: number;
    judge_model: string;
};

type Trend = EvalTrendData & { rubric: Rubric | null };

type Orchestration = { id: string; name: string; rubric_id?: string };

type Case = { id?: string; name: string; input: string; notes: string };

type Suite = {
    id: string;
    name: string;
    orchestration_id: string;
    cases: Case[];
    estimate: { cases: number; runs: number; judge_calls: number };
    running: boolean;
};

type SuiteRun = {
    id: string;
    status: string;
    case_count: number;
    scored_count: number;
    mean_score: number | null;
    /** "" when the workflow had no rubric: its cases ran and nothing was scored. */
    rubric_id: string;
    rubric_version: number;
    started_at: string;
};

type ToastFn = (message: string, tone?: "success" | "warning" | "danger") => void;

/** What the detail shows: a rubric, a set, or a new one of either. */
type Open = { kind: "rubric" | "set"; id: string } | { kind: "new-rubric" | "new-set" };

/** The judge-model picker's "nothing chosen" row: blank means the workspace's, then the default. */
const JUDGE_DEFAULT = "__default__";

type RubricDraft = Omit<Rubric, "id" | "version"> & { id?: string; version?: number };

const BLANK: Omit<Rubric, "id" | "version"> = {
    name: "",
    description: "",
    criteria: [{ name: "", guidance: "" }],
    scale: { min: 0, max: 10 },
    floor: 6,
    drop: 1.5,
    baseline_runs: 10,
    judge_model: "",
};

const HEAD = "flex items-center justify-between gap-2 px-1 pb-1.5 pt-1";
const HEAD_TITLE = "m-0 text-[13px] font-semibold text-text";
const SUB_LABEL = "block text-sm font-medium text-text";

export function EvalsScreen() {
    const { toast, show, dismiss } = useToast();

    const [rubrics, setRubrics] = useState<Rubric[]>([]);
    const [orchestrations, setOrchestrations] = useState<Orchestration[]>([]);
    const [suites, setSuites] = useState<Suite[]>([]);
    const [loading, setLoading] = useState(true);
    const [picked, setPicked] = useState<Open | null>(null);
    const detailRef = useRef<HTMLElement>(null);

    const refresh = useCallback(async () => {
        try {
            const [r, o, s] = await Promise.all([
                fetch("/api/rubrics").then((res) => (res.ok ? res.json() : [])),
                fetch("/api/orchestrations").then((res) => (res.ok ? res.json() : [])),
                fetch("/api/evals/suites").then((res) => (res.ok ? res.json() : [])),
            ]);
            setRubrics(r);
            setOrchestrations(o);
            setSuites(s);
        } catch {
            setRubrics([]);
            setOrchestrations([]);
            setSuites([]);
        } finally {
            setLoading(false);
        }
    }, []);

    const refreshSuites = useCallback(async () => {
        try {
            const res = await fetch("/api/evals/suites");
            setSuites(res.ok ? await res.json() : []);
        } catch {
            setSuites([]);
        }
    }, []);

    useEffect(() => {
        void refresh();
    }, [refresh]);

    // While a set runs, its tally is written as each case lands; a poll turns that
    // into progress somebody can watch. It runs while any set runs, open or not
    // (F204: a set started and left kept its Stop button after it had finished).
    const anyRunning = suites.some((s) => s.running);
    useEffect(() => {
        if (!anyRunning) return;
        const timer = setInterval(() => void refreshSuites(), 2500);
        return () => clearInterval(timer);
    }, [anyRunning, refreshSuites]);

    // The detail never opens empty: the first rubric, else the first set.
    const stillThere = (o: Open | null): o is Open => {
        if (!o) return false;
        if (o.kind === "rubric") return rubrics.some((r) => r.id === o.id);
        if (o.kind === "set") return suites.some((x) => x.id === o.id);
        return true;
    };
    const open: Open | null = stillThere(picked)
        ? picked
        : rubrics[0] ? { kind: "rubric", id: rubrics[0].id } : suites[0] ? { kind: "set", id: suites[0].id } : null;

    const choose = (next: Open) => {
        setPicked(next);
        // Below the two columns the detail sits under the list: bring it into view.
        if (typeof window !== "undefined" && window.matchMedia("(max-width: 1023px)").matches) {
            requestAnimationFrame(() => detailRef.current?.scrollIntoView({ block: "start", behavior: "smooth" }));
        }
    };

    const openRubric = open?.kind === "rubric" ? rubrics.find((r) => r.id === open.id) ?? null : null;
    const openSuite = open?.kind === "set" ? suites.find((s) => s.id === open.id) ?? null : null;
    const isOpen = (kind: "rubric" | "set", id: string) => open?.kind === kind && "id" in open && open.id === id;

    return (
        <AppScreen id="evals" width="wide">
            {loading ? (
                <Muted>Loading…</Muted>
            ) : (
                <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,19rem)_minmax(0,1fr)]">
                    {/* ── the list ─────────────────────────────────────────── */}
                    <nav aria-label="Rubrics and golden sets" className="grid gap-6">
                        <div>
                            <div className={HEAD}>
                                <h2 className={HEAD_TITLE}>Rubrics</h2>
                                <Button size="sm" variant="secondary" onClick={() => choose({ kind: "new-rubric" })}>
                                    <Plus className="h-3.5 w-3.5" aria-hidden /> New rubric
                                </Button>
                            </div>
                            {rubrics.length === 0 ? (
                                <p className="m-0 border-t border-border px-1 py-3 text-[13px] text-text-faint">No rubrics yet.</p>
                            ) : (
                                <ul className="m-0 list-none border-t border-border p-0">
                                    {rubrics.map((rubric) => {
                                        const bound = orchestrations.filter((o) => o.rubric_id === rubric.id);
                                        return (
                                            <ListRow
                                                key={rubric.id}
                                                data={{ "data-eval-rubric": rubric.id }}
                                                current={isOpen("rubric", rubric.id)}
                                                onClick={() => choose({ kind: "rubric", id: rubric.id })}
                                                title={rubric.name}
                                                meta={`v${rubric.version}`}
                                                sub={`${count(rubric.criteria.length, "criterion", "criteria")} · ${
                                                    bound.length === 0 ? "not bound" : `scoring ${count(bound.length, "workflow")}`}`}
                                            />
                                        );
                                    })}
                                </ul>
                            )}
                        </div>

                        <div>
                            <div className={HEAD}>
                                <h2 className={HEAD_TITLE}>Golden sets</h2>
                                <Button size="sm" variant="secondary" onClick={() => choose({ kind: "new-set" })}>
                                    <Plus className="h-3.5 w-3.5" aria-hidden /> New set
                                </Button>
                            </div>
                            {suites.length === 0 ? (
                                <p className="m-0 border-t border-border px-1 py-3 text-[13px] text-text-faint">No golden sets yet.</p>
                            ) : (
                                <ul className="m-0 list-none border-t border-border p-0">
                                    {suites.map((suite) => (
                                        <ListRow
                                            key={suite.id}
                                            data={{ "data-eval-set": suite.id }}
                                            current={isOpen("set", suite.id)}
                                            onClick={() => choose({ kind: "set", id: suite.id })}
                                            title={suite.name}
                                            meta={suite.running ? <span className="text-accent-ink">Running</span> : undefined}
                                            sub={`${count(suite.estimate.cases, "case")} · ${
                                                orchestrations.find((o) => o.id === suite.orchestration_id)?.name ?? "no workflow"}`}
                                        />
                                    ))}
                                </ul>
                            )}
                        </div>
                    </nav>

                    {/* ── the open one ─────────────────────────────────────── */}
                    <section ref={detailRef} aria-label="Detail" className="min-w-0 scroll-mt-4">
                        {open === null ? (
                            <EmptyState
                                title="No rubrics yet"
                                action={
                                    <Button size="sm" onClick={() => choose({ kind: "new-rubric" })}>
                                        <Plus className="h-3.5 w-3.5" aria-hidden /> New rubric
                                    </Button>
                                }
                            >
                                A rubric says what good looks like. Bind one to a workflow and every run gets
                                scored; a golden set holds the inputs still, so a change in the score is a change
                                in the workflow rather than in the questions.
                            </EmptyState>
                        ) : open.kind === "rubric" || open.kind === "new-rubric" ? (
                            <RubricDetail
                                key={openRubric ? `${openRubric.id}:${openRubric.version}` : "new"}
                                rubric={openRubric}
                                rubrics={rubrics}
                                orchestrations={orchestrations}
                                onToast={show}
                                onSaved={async (id) => { await refresh(); setPicked({ kind: "rubric", id }); }}
                                onDeleted={async () => { setPicked(null); await refresh(); }}
                                onCancelNew={() => setPicked(null)}
                            />
                        ) : (
                            <SetDetail
                                key={openSuite?.id ?? "new"}
                                suite={openSuite}
                                orchestrations={orchestrations}
                                onToast={show}
                                onChanged={refreshSuites}
                                onSaved={async (id) => { await refreshSuites(); setPicked({ kind: "set", id }); }}
                                onDeleted={async () => { setPicked(null); await refreshSuites(); }}
                                onCancelNew={() => setPicked(null)}
                            />
                        )}
                    </section>
                </div>
            )}
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </AppScreen>
    );
}

function RubricDetail({
    rubric, rubrics, orchestrations, onToast, onSaved, onDeleted, onCancelNew,
}: {
    rubric: Rubric | null;
    rubrics: Rubric[];
    orchestrations: Orchestration[];
    onToast: ToastFn;
    onSaved: (id: string) => Promise<void>;
    onDeleted: () => Promise<void>;
    onCancelNew: () => void;
}) {
    const fresh = (): RubricDraft => (rubric ? { ...rubric } : { ...BLANK, criteria: [{ name: "", guidance: "" }] });
    const [draft, setDraft] = useState<RubricDraft>(fresh);
    const [busy, setBusy] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const { models } = useSettingsData();

    // "Scores for": this rubric's workflows first, then the rest, with why they aren't scored by it.
    const bound = rubric ? orchestrations.filter((o) => o.rubric_id === rubric.id) : [];
    const others = rubric ? orchestrations.filter((o) => o.rubric_id !== rubric.id) : [];
    const [orchId, setOrchId] = useState<string>(bound[0]?.id ?? "");
    const [trend, setTrend] = useState<Trend | null>(null);
    const [trendBusy, setTrendBusy] = useState(false);
    const [backfilling, setBackfilling] = useState(false);

    const loadTrend = useCallback(async (id: string) => {
        if (!id) {
            setTrend(null);
            return;
        }
        setTrendBusy(true);
        try {
            const res = await fetch(`/api/evals/orchestrations/${encodeURIComponent(id)}`);
            setTrend(res.ok ? await res.json() : null);
        } catch {
            setTrend(null);
        } finally {
            setTrendBusy(false);
        }
    }, []);

    useEffect(() => {
        void loadTrend(orchId);
    }, [orchId, loadTrend]);

    async function saveRubric() {
        setBusy(true);
        try {
            const body = {
                name: draft.name,
                description: draft.description,
                criteria: draft.criteria.filter((c) => c.name.trim()),
                scale: draft.scale,
                floor: draft.floor,
                drop: draft.drop,
                baseline_runs: draft.baseline_runs,
                judge_model: draft.judge_model,
            };
            const res = draft.id
                ? await fetch(`/api/rubrics/${draft.id}`, {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                })
                : await fetch("/api/rubrics", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
            if (!res.ok) throw new Error(await res.text());
            const saved: Rubric = await res.json();
            const bumped = draft.version !== undefined && saved.version > draft.version;
            onToast(
                bumped
                    ? `Saved as version ${saved.version}. Earlier scores stay on version ${draft.version}.`
                    : "Rubric saved.",
                bumped ? "warning" : "success",
            );
            await onSaved(saved.id);
        } catch {
            onToast("Could not save the rubric.", "danger");
        } finally {
            setBusy(false);
        }
    }

    async function removeRubric() {
        if (!rubric) return;
        try {
            const res = await fetch(`/api/rubrics/${rubric.id}`, { method: "DELETE" });
            if (!res.ok) throw new Error();
            onToast("Rubric deleted. Scores taken under it are kept.", "success");
            setConfirmDelete(false);
            await onDeleted();
        } catch {
            onToast("Could not delete the rubric.", "danger");
        }
    }

    // Scores the runs a workflow already has, which the empty state used to
    // promise with no way to do it but the API (F204).
    async function backfill(id: string) {
        setBackfilling(true);
        try {
            const res = await fetch(`/api/evals/orchestrations/${encodeURIComponent(id)}/backfill`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: "{}",
            });
            if (!res.ok) throw new Error();
            const { requested, scored } = (await res.json()) as { requested: number; scored: number };
            if (requested === 0) {
                onToast("No finished runs to score yet. Run it first.", "warning");
            } else {
                onToast(
                    `Scored ${scored} of ${count(requested, "run")}.`,
                    scored === requested ? "success" : "warning",
                );
            }
            await loadTrend(id);
        } catch {
            onToast("Could not score the earlier runs.", "danger");
        } finally {
            setBackfilling(false);
        }
    }

    const scaleMax = trend?.rubric?.scale?.max ?? rubric?.scale.max ?? 10;
    const picked = orchestrations.find((o) => o.id === orchId);
    const judgeOptions = [
        { value: JUDGE_DEFAULT, label: "Workspace judge model, or the default model" },
        ...withUnreachable(modelOptions(models.providers ?? {}), draft.judge_model || undefined),
    ];
    const hintOf = (o: Orchestration) =>
        !o.rubric_id ? "no rubric bound" : rubric && o.rubric_id !== rubric.id ? "scored by another rubric" : undefined;

    return (
        <div className="grid gap-8">
            <DetailHead
                title={rubric ? rubric.name : "New rubric"}
                sub={rubric && (
                    <>
                        <span className="tabular-nums">v{rubric.version}</span>
                        {" · "}
                        {count(rubric.criteria.length, "criterion", "criteria")}
                        {" · "}
                        {bound.length === 0 ? "Not bound" : `Scores ${count(bound.length, "workflow")}`}
                    </>
                )}
                actions={rubric && (
                    <IconButton label="Delete rubric" icon={Trash} tone="danger" onClick={() => setConfirmDelete(true)} />
                )}
            />

            {rubric && (
                <div className="grid gap-6">
                    <div className="max-w-[28rem]">
                        <Field label="Scores for">
                            <Select
                                value={orchId || undefined}
                                onChange={setOrchId}
                                placeholder={orchestrations.length ? "Pick a workflow…" : "No workflows yet"}
                                options={[...bound, ...others].map((o) => ({ value: o.id, label: o.name, hint: hintOf(o) }))}
                            />
                        </Field>
                    </div>

                    {trendBusy ? (
                        <Muted>Loading…</Muted>
                    ) : !orchId ? (
                        <EmptyState title="Pick a workflow">
                            Scores appear here once a rubric is bound to it and it has run.
                        </EmptyState>
                    ) : !trend || trend.scores.length === 0 ? (
                        picked?.rubric_id ? (
                            <EmptyState
                                title="Nothing scored yet"
                                action={
                                    <Button size="sm" variant="secondary"
                                            onClick={() => backfill(orchId)} disabled={backfilling}>
                                        <Target className="h-3.5 w-3.5" aria-hidden />
                                        {backfilling ? "Scoring…" : "Score earlier runs"}
                                    </Button>
                                }
                            >
                                Every run from now on is scored. Runs it already has can be scored too.
                            </EmptyState>
                        ) : (
                            <EmptyState title="Nothing scored yet">
                                No rubric is bound to this workflow. Bind one in its workflow settings, under
                                “Score runs against”, and its runs are scored from then on.
                            </EmptyState>
                        )
                    ) : (
                        <EvalTrend
                            trend={trend}
                            scaleMax={scaleMax}
                            onRate={async (runId, verdict) => {
                                await fetch("/api/evals/feedback", {
                                    method: "POST",
                                    headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ run_id: runId, verdict, user_ref: "me" }),
                                });
                                await loadTrend(orchId);
                            }}
                        />
                    )}
                </div>
            )}

            <Section variant="plain" collapsible={false} title="Rubric" description="What good looks like, and when a run is flagged.">
                <div className="grid max-w-[48rem] gap-4">
                    <Field label="Name">
                        <Input
                            value={draft.name}
                            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                            placeholder="Research quality"
                        />
                    </Field>

                    <Field
                        label="What the judge should keep in mind"
                        hint="Context that applies to every criterion."
                    >
                        <Textarea
                            rows={3}
                            value={draft.description}
                            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                            placeholder="This workflow produces market briefs for analysts. Prefer specifics over hedging."
                        />
                    </Field>

                    <div className="space-y-2">
                        <span className={SUB_LABEL}>Criteria</span>
                        {draft.criteria.map((criterion, index) => (
                            <div key={index} className="flex gap-2">
                                <Input
                                    className="w-40"
                                    aria-label={`Criterion ${index + 1} name`}
                                    value={criterion.name}
                                    placeholder="Sources"
                                    onChange={(e) => {
                                        const next = [...draft.criteria];
                                        next[index] = { ...criterion, name: e.target.value };
                                        setDraft({ ...draft, criteria: next });
                                    }}
                                />
                                <Input
                                    className="flex-1"
                                    aria-label={`Criterion ${index + 1} guidance`}
                                    value={criterion.guidance ?? ""}
                                    placeholder="At least three cited URLs"
                                    onChange={(e) => {
                                        const next = [...draft.criteria];
                                        next[index] = { ...criterion, guidance: e.target.value };
                                        setDraft({ ...draft, criteria: next });
                                    }}
                                />
                                <IconButton
                                    label="Remove criterion"
                                    icon={Trash}
                                    onClick={() =>
                                        setDraft({
                                            ...draft,
                                            criteria: draft.criteria.filter((_, i) => i !== index),
                                        })
                                    }
                                />
                            </div>
                        ))}
                        <Button
                            size="sm"
                            variant="ghost"
                            onClick={() =>
                                setDraft({ ...draft, criteria: [...draft.criteria, { name: "", guidance: "" }] })
                            }
                        >
                            <Plus className="h-3.5 w-3.5" aria-hidden /> Add criterion
                        </Button>
                        {draft.version !== undefined && (
                            <Muted className="block text-xs">
                                Changing a criterion or the scale starts version {draft.version + 1}.
                                Earlier scores stay on version {draft.version} and are not rewritten —
                                the trend shows a break rather than comparing across the change.
                            </Muted>
                        )}
                    </div>

                    <div className="grid gap-3 sm:grid-cols-3">
                        <Field label="Scale max">
                            <Input
                                type="number"
                                value={draft.scale.max}
                                onChange={(e) =>
                                    setDraft({
                                        ...draft,
                                        scale: { ...draft.scale, max: Number(e.target.value) },
                                    })
                                }
                            />
                        </Field>
                        <Field label="Floor" hint="Flag anything below this. Blank to disable.">
                            <Input
                                type="number"
                                value={draft.floor ?? ""}
                                onChange={(e) =>
                                    setDraft({
                                        ...draft,
                                        floor: e.target.value === "" ? null : Number(e.target.value),
                                    })
                                }
                            />
                        </Field>
                        <Field label="Drop" hint="Flag a fall this far below recent runs.">
                            <Input
                                type="number"
                                value={draft.drop ?? ""}
                                onChange={(e) =>
                                    setDraft({
                                        ...draft,
                                        drop: e.target.value === "" ? null : Number(e.target.value),
                                    })
                                }
                            />
                        </Field>
                    </div>

                    <Field
                        label="Judge model"
                        hint="The model that scores each run against this rubric."
                    >
                        <Combobox
                            // "default", as the free-text box suggested, means the same as blank.
                            value={!draft.judge_model || draft.judge_model === "default" ? JUDGE_DEFAULT : draft.judge_model}
                            onChange={(v) => setDraft({ ...draft, judge_model: v === JUDGE_DEFAULT ? "" : v })}
                            options={judgeOptions}
                            searchPlaceholder="Search models…"
                            aria-label="Judge model"
                        />
                    </Field>

                    <div className="flex gap-2">
                        <Button onClick={saveRubric} disabled={busy || !draft.name.trim()}>
                            {busy ? "Saving…" : "Save rubric"}
                        </Button>
                        {/* A new rubric is dropped; a saved one goes back to what was saved. */}
                        <Button variant="ghost" onClick={() => (rubric ? setDraft(fresh()) : onCancelNew())}>
                            Cancel
                        </Button>
                    </div>
                </div>
            </Section>

            <ConfirmDialog
                open={confirmDelete}
                onClose={() => setConfirmDelete(false)}
                title={`Delete “${rubric?.name ?? rubrics.find((r) => r.id === draft.id)?.name}”?`}
                description="Scores already taken under this rubric are kept — they are the record of what was judged. Workflows bound to it stop being scored."
                confirmLabel="Delete"
                onConfirm={() => removeRubric()}
            />
        </div>
    );
}

/**
 * A golden set — saved inputs, re-run on demand.
 *
 * Scoring live runs measures quality against whatever customers happened to ask
 * that week; holding the inputs still means a change in the score is a change in
 * the *workflow*. The cost of a run is said **before** the button does anything
 * (the most expensive action in the product, and the only one whose price is
 * known in advance), and suggested cases arrive as suggestions to accept, never
 * saved on arrival: a set the model wrote unattended tests what it finds easy.
 */
function SetDetail({
    suite, orchestrations, onToast, onChanged, onSaved, onDeleted, onCancelNew,
}: {
    suite: Suite | null;
    orchestrations: Orchestration[];
    onToast: ToastFn;
    onChanged: () => Promise<void>;
    onSaved: (id: string) => Promise<void>;
    onDeleted: () => Promise<void>;
    onCancelNew: () => void;
}) {
    type Draft = Omit<Suite, "id" | "estimate" | "running"> & { id?: string };
    const fresh = (): Draft => (suite ? { ...suite } : { id: undefined, name: "", orchestration_id: "", cases: [] });
    const [draft, setDraft] = useState<Draft>(fresh);
    const [proposed, setProposed] = useState<Case[] | null>(null);
    const [busy, setBusy] = useState(false);
    const [history, setHistory] = useState<SuiteRun[] | null>(null);
    const [confirmRun, setConfirmRun] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const clock = useClock();

    const suiteId = suite?.id;
    const loadHistory = useCallback(async () => {
        if (!suiteId) return;
        try {
            const res = await fetch(`/api/evals/suites/${suiteId}/runs`);
            setHistory(res.ok ? await res.json() : []);
        } catch {
            setHistory([]);
        }
    }, [suiteId]);

    const running = !!suite?.running;
    useEffect(() => {
        void loadHistory();
        if (!running) return;
        const timer = setInterval(() => void loadHistory(), 2500);
        return () => clearInterval(timer);
    }, [running, loadHistory]);

    async function save() {
        setBusy(true);
        try {
            const body = {
                name: draft.name,
                orchestration_id: draft.orchestration_id,
                cases: draft.cases.filter((c) => c.input.trim()),
            };
            const res = draft.id
                ? await fetch(`/api/evals/suites/${draft.id}`, {
                    method: "PUT", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                })
                : await fetch("/api/evals/suites", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body),
                });
            if (!res.ok) throw new Error();
            const saved = await res.json().catch(() => null);
            onToast("Set saved.");
            setProposed(null);
            await onSaved(saved?.id ?? draft.id ?? "");
        } catch {
            onToast("Could not save the set.", "danger");
        } finally {
            setBusy(false);
        }
    }

    async function propose(suiteId: string) {
        setBusy(true);
        try {
            const res = await fetch(`/api/evals/suites/${suiteId}/generate`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ count: 5 }),
            });
            if (!res.ok) throw new Error();
            const body = await res.json();
            if (!body.cases.length) {
                onToast("The model did not return any usable cases.", "warning");
            }
            setProposed(body.cases);
        } catch {
            onToast("Could not generate cases.", "danger");
        } finally {
            setBusy(false);
        }
    }

    // Asked in the kit's dialog, not window.confirm (F204). With no rubric bound
    // the cases run and nothing is scored, so it says that rather than pricing
    // judge calls that will never be made.
    async function start() {
        if (!suite) return;
        setConfirmRun(false);
        const scored = !!orchestrations.find((o) => o.id === suite.orchestration_id)?.rubric_id;
        try {
            const res = await fetch(`/api/evals/suites/${suite.id}/run`, { method: "POST" });
            if (!res.ok) throw new Error((await res.json()).detail);
            onToast(scored
                ? "Running. Cases are scored as they finish."
                : "Running. No rubric is bound, so the cases will not be scored.",
                scored ? "success" : "warning");
            await onChanged();
        } catch (e) {
            onToast(e instanceof Error && e.message ? e.message : "Could not start the set.", "danger");
        }
    }

    async function stop() {
        if (!suite) return;
        try {
            await fetch(`/api/evals/suites/${suite.id}/cancel`, { method: "POST" });
            onToast("Stopping after the case in flight. What already ran is kept.", "warning");
            await onChanged();
        } catch {
            onToast("Could not stop the set.", "danger");
        }
    }

    // A set could not be deleted here (F508), only through the API. Never while it runs: the
    // route refuses, and the button waits for Stop.
    async function removeSet() {
        if (!suite) return;
        try {
            const res = await fetch(`/api/evals/suites/${suite.id}`, { method: "DELETE" });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                throw new Error(typeof body?.detail === "string" ? body.detail : `the server answered ${res.status}`);
            }
            onToast("Set deleted. Its past runs are kept.", "success");
            setConfirmDelete(false);
            await onDeleted();
        } catch (e) {
            setConfirmDelete(false);
            onToast(`Could not delete the set: ${e instanceof Error ? e.message : "it was refused"}`, "danger");
        }
    }

    const latest = history?.[0];
    const workflow = orchestrations.find((o) => o.id === suite?.orchestration_id);

    return (
        <div className="grid gap-8">
            <DetailHead
                title={suite ? suite.name : "New set"}
                sub={suite && `${count(suite.estimate.cases, "case")} · ${workflow?.name ?? "no workflow"}${
                    workflow && !workflow.rubric_id ? " (no rubric bound)" : ""}`}
                actions={suite && (
                    <>
                        {suite.running ? (
                            <Button size="sm" variant="secondary" onClick={stop}>
                                <Square className="h-3.5 w-3.5" aria-hidden /> Stop
                            </Button>
                        ) : (
                            <Button size="sm" onClick={() => setConfirmRun(true)} disabled={suite.estimate.cases === 0}>
                                <Play className="h-3.5 w-3.5" aria-hidden /> Run
                            </Button>
                        )}
                        <IconButton
                            label="Delete set"
                            title={suite.running ? "Stop the set's run first" : "Delete set"}
                            icon={Trash}
                            tone="danger"
                            disabled={suite.running}
                            onClick={() => setConfirmDelete(true)}
                        />
                    </>
                )}
            />

            {suite && (
                <section aria-labelledby="set-runs" className="grid gap-1">
                    <h3 id="set-runs" className="m-0 text-[13px] font-semibold text-text">Runs of this set</h3>
                    {suite.running && latest && (
                        <p className="m-0 text-xs text-accent-ink">
                            Running — {latest.scored_count} of {latest.case_count} scored.
                        </p>
                    )}
                    {history === null ? (
                        <Muted className="block text-xs">Loading…</Muted>
                    ) : history.length === 0 ? (
                        <p className="m-0 border-t border-border py-3 text-[13px] text-text-faint">Never run.</p>
                    ) : (
                        <ul aria-label="Runs of this set" className="m-0 list-none border-t border-border p-0">
                            {history.map((run) => (
                                <li key={run.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-1 py-2.5 text-[13px]">
                                    <span className="w-10 font-semibold tabular-nums text-text">
                                        {run.mean_score === null ? "—" : run.mean_score.toFixed(1)}
                                    </span>
                                    {run.rubric_id ? (
                                        <>
                                            <span className="tabular-nums text-text-muted">
                                                {run.scored_count}/{run.case_count} scored
                                            </span>
                                            <span className="rounded bg-surface-2 px-1.5 text-[11.5px] font-medium tabular-nums text-text-muted">
                                                v{run.rubric_version}
                                            </span>
                                        </>
                                    ) : (
                                        <span className="text-text-muted">not scored: no rubric</span>
                                    )}
                                    {run.status !== "completed" && (
                                        <span className={run.status === "failed" ? "font-medium text-danger" : "text-text-muted"}>
                                            {run.status}
                                        </span>
                                    )}
                                    <When iso={run.started_at} clock={clock} className="ml-auto text-xs tabular-nums text-text-faint" />
                                </li>
                            ))}
                        </ul>
                    )}
                </section>
            )}

            <Section variant="plain" collapsible={false} title="Set" description="The inputs it sends, one run each.">
                <div className="grid max-w-[48rem] gap-4">
                    <Field label="Name">
                        <Input
                            value={draft.name}
                            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                            placeholder="Regression set"
                        />
                    </Field>
                    <Field label="Workflow">
                        <Select
                            value={draft.orchestration_id || undefined}
                            onChange={(v) => setDraft({ ...draft, orchestration_id: v })}
                            placeholder="Pick one…"
                            options={orchestrations.map((o) => ({
                                value: o.id, label: o.name,
                                hint: o.rubric_id ? undefined : "no rubric — cases will run but not be scored",
                            }))}
                        />
                    </Field>

                    <div className="space-y-2">
                        <div className="flex items-center justify-between">
                            <span className={SUB_LABEL}>Cases</span>
                            {draft.id && (
                                <Button size="sm" variant="ghost" disabled={busy}
                                        onClick={() => propose(draft.id!)}>
                                    <Sparkles className="h-3.5 w-3.5" aria-hidden />
                                    {busy ? "Thinking…" : "Suggest cases"}
                                </Button>
                            )}
                        </div>

                        {proposed && (
                            <div className="space-y-2 rounded-md border border-border bg-machine-subtle/60 p-3">
                                <Muted className="block text-xs">
                                    Suggestions only — nothing is saved until you add them.
                                </Muted>
                                {proposed.map((c, i) => (
                                    <div key={i} className="flex items-start gap-2 text-xs">
                                        <div className="flex-1">
                                            <div className="text-text">{c.name || "Untitled"}</div>
                                            <Muted className="block">{c.input}</Muted>
                                        </div>
                                        <Button size="sm" variant="ghost" onClick={() => {
                                            setDraft({ ...draft, cases: [...draft.cases, c] });
                                            setProposed(proposed.filter((_, j) => j !== i));
                                        }}>
                                            Add
                                        </Button>
                                    </div>
                                ))}
                                <Button size="sm" variant="ghost" onClick={() => setProposed(null)}>
                                    Dismiss
                                </Button>
                            </div>
                        )}

                        {draft.cases.map((c, index) => (
                            <div key={index} className="flex gap-2">
                                <Input
                                    className="w-40"
                                    aria-label={`Case ${index + 1} name`}
                                    placeholder="Label"
                                    value={c.name}
                                    onChange={(e) => {
                                        const next = [...draft.cases];
                                        next[index] = { ...c, name: e.target.value };
                                        setDraft({ ...draft, cases: next });
                                    }}
                                />
                                <Input
                                    className="flex-1"
                                    aria-label={`Case ${index + 1} input`}
                                    placeholder="The input this case sends"
                                    value={c.input}
                                    onChange={(e) => {
                                        const next = [...draft.cases];
                                        next[index] = { ...c, input: e.target.value };
                                        setDraft({ ...draft, cases: next });
                                    }}
                                />
                                <IconButton
                                    label="Remove case"
                                    icon={Trash}
                                    onClick={() =>
                                        setDraft({ ...draft, cases: draft.cases.filter((_, i) => i !== index) })
                                    }
                                />
                            </div>
                        ))}
                        <Button size="sm" variant="ghost"
                                onClick={() => setDraft({ ...draft, cases: [...draft.cases, { name: "", input: "", notes: "" }] })}>
                            <Plus className="h-3.5 w-3.5" aria-hidden /> Add case
                        </Button>
                    </div>

                    <div className="flex gap-2">
                        <Button onClick={save} disabled={busy || !draft.name.trim() || !draft.orchestration_id}>
                            {busy ? "Saving…" : "Save set"}
                        </Button>
                        {/* A new set is dropped; a saved one goes back to what was saved. */}
                        <Button variant="ghost" onClick={() => { setProposed(null); if (suite) setDraft(fresh()); else onCancelNew(); }}>
                            Cancel
                        </Button>
                    </div>
                </div>
            </Section>

            <ConfirmDialog
                open={confirmRun}
                onClose={() => setConfirmRun(false)}
                title={suite?.estimate.runs === 1 ? "Run its one case?" : `Run all ${suite?.estimate.runs ?? 0} cases?`}
                description={suite ? runCost(suite, orchestrations) : undefined}
                confirmLabel="Run set"
                onConfirm={() => start()}
            />
            <ConfirmDialog
                open={confirmDelete}
                onClose={() => setConfirmDelete(false)}
                title={`Delete “${suite?.name ?? "this set"}”?`}
                description="Its cases go with it. Its past runs stay in Runs, with their scores."
                confirmLabel="Delete"
                onConfirm={() => removeSet()}
            />
        </div>
    );
}

/** What running a set costs, said before it starts: it is the most expensive action here. */
function runCost(suite: Suite, orchestrations: Orchestration[]): string {
    const orch = orchestrations.find((o) => o.id === suite.orchestration_id);
    const name = orch?.name ?? "the workflow";
    const { runs, judge_calls } = suite.estimate;
    const runsText = `${count(runs, "full run")} of “${name}”`;
    if (!orch?.rubric_id) {
        return `That is ${runsText}, billed as usual. No rubric is bound to it, so they will not be scored: ` +
            "bind one in its workflow settings first to score them. Cases run one at a time and you can stop it.";
    }
    return `That is ${runsText} plus ${count(judge_calls, "judge call")}, all billed as usual. ` +
        "Cases run one at a time and you can stop it.";
}
