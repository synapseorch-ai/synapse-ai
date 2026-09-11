"use client";

/*
 * /evals — rubrics, and what the judge thought of every run measured against one.
 *
 * Note the vocabulary, because the product already uses the other word: an
 * **Evaluator** is a routing step inside an orchestration and has nothing to do
 * with this screen. What lives here is the **judge** — a model that scores a
 * finished run against a rubric you wrote.
 *
 * Two panes. Left is the library of rubrics; right is either the rubric editor
 * or, once an orchestration is picked, its score over time with the
 * regressions called out. The trend deliberately shows a break where the
 * rubric version changed: scores either side of one are answers to different
 * questions, and drawing a continuous line through that point would invent a
 * regression (or hide one).
 */
import { useCallback, useEffect, useState } from "react";
import { ListChecks, Play, Plus, Sparkles, Square, Target, ThumbsDown, ThumbsUp, Trash } from "lucide-react";

import {
    Badge, Button, Card, CardBody, ConfirmDialog, EmptyState, Field, IconButton,
    Input, Label, Muted, Select, Stat, Textarea, Toast, useToast,
} from "@/components/ui";

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

type Score = {
    id: number;
    run_id: string;
    orchestration_id: string;
    rubric_id: string;
    rubric_version: number;
    score: number;
    per_criterion: Record<string, { score: number; reasoning: string }>;
    per_step: Record<string, { score: number; reasoning: string }>;
    reasoning: string;
    judged_at: string;
    judge_model: string;
    baseline: number | null;
    regression: boolean;
    rule: string | null;
};

type Trend = {
    scores: Score[];
    feedback: Record<string, { verdict: number; user_ref: string }[]>;
    rubric: Rubric | null;
    agreement: { compared: number; agreed: number; rate: number | null };
    regressions: Score[];
};

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
    rubric_version: number;
    started_at: string;
};

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

/** How many run cards the trend lists. The chart above them carries the shape
 *  of the whole window; a hundred identical cards carry nothing and bury what
 *  is underneath them. */
const RUNS_SHOWN = 20;

/** The colour band a score reads in. Thirds of the rubric's own scale, so a
 *  0-5 rubric and a 0-10 rubric shade the same proportion the same way. */
export function scoreTone(score: number | null | undefined, max = 10): "neutral" | "success" | "warning" | "danger" {
    if (score === null || score === undefined) return "neutral";
    const share = max > 0 ? score / max : 0;
    if (share >= 0.75) return "success";
    if (share >= 0.5) return "warning";
    return "danger";
}

export function EvalsScreen() {
    const { toast, show, dismiss } = useToast();

    const [rubrics, setRubrics] = useState<Rubric[]>([]);
    const [orchestrations, setOrchestrations] = useState<Orchestration[]>([]);
    const [loading, setLoading] = useState(true);

    const [draft, setDraft] = useState<(Omit<Rubric, "id" | "version"> & { id?: string; version?: number }) | null>(null);
    const [busy, setBusy] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState<Rubric | null>(null);

    const [orchId, setOrchId] = useState<string>("");
    const [trend, setTrend] = useState<Trend | null>(null);
    const [trendBusy, setTrendBusy] = useState(false);

    const refresh = useCallback(async () => {
        try {
            const [r, o] = await Promise.all([
                fetch("/api/rubrics").then((res) => (res.ok ? res.json() : [])),
                fetch("/api/orchestrations").then((res) => (res.ok ? res.json() : [])),
            ]);
            setRubrics(r);
            setOrchestrations(o);
        } catch {
            setRubrics([]);
            setOrchestrations([]);
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void refresh();
    }, [refresh]);

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
        if (!draft) return;
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
            show(
                bumped
                    ? `Saved as version ${saved.version}. Earlier scores stay on version ${draft.version}.`
                    : "Rubric saved.",
                bumped ? "warning" : "success",
            );
            setDraft(null);
            await refresh();
        } catch {
            show("Could not save the rubric.", "danger");
        } finally {
            setBusy(false);
        }
    }

    async function removeRubric(rubric: Rubric) {
        try {
            const res = await fetch(`/api/rubrics/${rubric.id}`, { method: "DELETE" });
            if (!res.ok) throw new Error();
            show("Rubric deleted. Scores taken under it are kept.", "success");
            setConfirmDelete(null);
            await refresh();
        } catch {
            show("Could not delete the rubric.", "danger");
        }
    }

    const scaleMax = trend?.rubric?.scale?.max ?? 10;

    return (
        <div className="grid gap-6 lg:grid-cols-[20rem_1fr]">
            {/* ── rubrics ─────────────────────────────────────────────── */}
            <div className="space-y-3">
                <div className="flex items-center justify-between">
                    <Label>Rubrics</Label>
                    <Button
                        size="sm"
                        onClick={() => setDraft({ ...BLANK, criteria: [{ name: "", guidance: "" }] })}
                    >
                        <Plus className="h-3.5 w-3.5" /> New
                    </Button>
                </div>

                {loading ? (
                    <Muted>Loading…</Muted>
                ) : rubrics.length === 0 ? (
                    <EmptyState title="No rubrics yet">
                        A rubric says what good looks like. Bind one to an orchestration
                        and every run gets scored.
                    </EmptyState>
                ) : (
                    <ul className="space-y-2">
                        {rubrics.map((rubric) => {
                            const bound = orchestrations.filter((o) => o.rubric_id === rubric.id);
                            return (
                                <li key={rubric.id}>
                                    <button
                                        type="button"
                                        onClick={() => setDraft({ ...rubric })}
                                        className="w-full rounded-md border border-border bg-surface px-3 py-2 text-left hover:border-accent/50"
                                    >
                                        <div className="flex items-center justify-between gap-2">
                                            <span className="truncate text-sm">{rubric.name}</span>
                                            <Badge size="sm">v{rubric.version}</Badge>
                                        </div>
                                        <Muted className="mt-0.5 block truncate text-xs">
                                            {rubric.criteria.length} criteria ·{" "}
                                            {bound.length === 0
                                                ? "not bound"
                                                : `scoring ${bound.length} orchestration${bound.length > 1 ? "s" : ""}`}
                                        </Muted>
                                    </button>
                                </li>
                            );
                        })}
                    </ul>
                )}

                <div className="pt-2">
                    <GoldenSets orchestrations={orchestrations} onToast={show} />
                </div>
            </div>

            {/* ── editor or trend ─────────────────────────────────────── */}
            <div className="space-y-4">
                {draft ? (
                    <Card>
                        <CardBody className="space-y-4">
                            <div className="flex items-center justify-between">
                                <Label>{draft.id ? "Edit rubric" : "New rubric"}</Label>
                                {draft.id && (
                                    <IconButton
                                        label="Delete rubric"
                                        icon={Trash}
                                        tone="danger"
                                        onClick={() => {
                                            const found = rubrics.find((r) => r.id === draft.id);
                                            if (found) setConfirmDelete(found);
                                        }}
                                    />
                                )}
                            </div>

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
                                <Label>Criteria</Label>
                                {draft.criteria.map((criterion, index) => (
                                    <div key={index} className="flex gap-2">
                                        <Input
                                            className="w-40"
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
                                    <Plus className="h-3.5 w-3.5" /> Add criterion
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
                                hint="Blank uses the workspace judge model, then the default model."
                            >
                                <Input
                                    value={draft.judge_model}
                                    onChange={(e) => setDraft({ ...draft, judge_model: e.target.value })}
                                    placeholder="default"
                                />
                            </Field>

                            <div className="flex gap-2">
                                <Button onClick={saveRubric} disabled={busy || !draft.name.trim()}>
                                    {busy ? "Saving…" : "Save rubric"}
                                </Button>
                                <Button variant="ghost" onClick={() => setDraft(null)}>
                                    Cancel
                                </Button>
                            </div>
                        </CardBody>
                    </Card>
                ) : (
                    <>
                        <Field label="Show scores for">
                            <Select
                                value={orchId || undefined}
                                onChange={setOrchId}
                                placeholder="Pick an orchestration…"
                                options={orchestrations.map((o) => ({
                                    value: o.id,
                                    label: o.name,
                                    hint: o.rubric_id ? undefined : "no rubric bound",
                                }))}
                            />
                        </Field>

                        {trendBusy ? (
                            <Muted>Loading…</Muted>
                        ) : !orchId ? (
                            <EmptyState title="Pick an orchestration">
                                Scores appear here once a rubric is bound to it and it has run.
                            </EmptyState>
                        ) : !trend || trend.scores.length === 0 ? (
                            <EmptyState title="Nothing scored yet">
                                Bind a rubric on the orchestration, then run it — or backfill
                                the runs it already has.
                            </EmptyState>
                        ) : (
                            <TrendView
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

                    </>
                )}
            </div>

            <ConfirmDialog
                open={confirmDelete !== null}
                onClose={() => setConfirmDelete(null)}
                title={`Delete “${confirmDelete?.name}”?`}
                description="Scores already taken under this rubric are kept — they are the record of what was judged. Orchestrations bound to it stop being scored."
                confirmLabel="Delete"
                onConfirm={() => confirmDelete && removeRubric(confirmDelete)}
            />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </div>
    );
}

/**
 * Golden sets — saved inputs, re-run on demand.
 *
 * The reason they exist: scoring live runs measures quality against whatever
 * customers happened to ask that week. Holding the inputs still means a change
 * in the score is a change in the *workflow*.
 *
 * Two things this screen is careful about. The cost of a run is shown **before**
 * the button does anything — it is the most expensive action in the product and
 * the only one whose price is knowable in advance. And generated cases arrive
 * as suggestions to edit and accept, never saved on arrival: a set the model
 * wrote unattended ends up testing what the model finds easy.
 */
function GoldenSets({
    orchestrations,
    onToast,
}: {
    orchestrations: Orchestration[];
    onToast: (message: string, tone?: "success" | "warning" | "danger") => void;
}) {
    const [suites, setSuites] = useState<Suite[]>([]);
    const [openId, setOpenId] = useState<string | null>(null);
    const [draft, setDraft] = useState<
        (Omit<Suite, "id" | "estimate" | "running"> & { id?: string }) | null
    >(null);
    const [proposed, setProposed] = useState<Case[] | null>(null);
    const [busy, setBusy] = useState(false);
    const [history, setHistory] = useState<SuiteRun[]>([]);

    const refresh = useCallback(async () => {
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

    const loadHistory = useCallback(async (id: string) => {
        try {
            const res = await fetch(`/api/evals/suites/${id}/runs`);
            setHistory(res.ok ? await res.json() : []);
        } catch {
            setHistory([]);
        }
    }, []);

    // While a suite is running the tally is written as each case lands, so a
    // poll is what turns that into progress somebody can watch.
    useEffect(() => {
        if (!openId) return;
        void loadHistory(openId);
        const running = suites.find((s) => s.id === openId)?.running;
        if (!running) return;
        const timer = setInterval(() => {
            void loadHistory(openId);
            void refresh();
        }, 2500);
        return () => clearInterval(timer);
    }, [openId, suites, loadHistory, refresh]);

    async function save() {
        if (!draft) return;
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
            onToast("Set saved.");
            setDraft(null);
            setProposed(null);
            await refresh();
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

    async function start(suite: Suite) {
        const { runs, judge_calls } = suite.estimate;
        const ok = window.confirm(
            `Run all ${runs} case${runs === 1 ? "" : "s"}?\n\n` +
            `That is ${runs} full run${runs === 1 ? "" : "s"} of “${
                orchestrations.find((o) => o.id === suite.orchestration_id)?.name ?? "the orchestration"
            }” plus ${judge_calls} judge call${judge_calls === 1 ? "" : "s"}, all billed as usual. ` +
            `Cases run one at a time and you can stop it.`,
        );
        if (!ok) return;
        try {
            const res = await fetch(`/api/evals/suites/${suite.id}/run`, { method: "POST" });
            if (!res.ok) throw new Error((await res.json()).detail);
            onToast("Running. Cases are scored as they finish.");
            await refresh();
        } catch (e) {
            onToast(e instanceof Error && e.message ? e.message : "Could not start the set.", "danger");
        }
    }

    async function stop(suite: Suite) {
        try {
            await fetch(`/api/evals/suites/${suite.id}/cancel`, { method: "POST" });
            onToast("Stopping after the case in flight. What already ran is kept.", "warning");
            await refresh();
        } catch {
            onToast("Could not stop the set.", "danger");
        }
    }

    if (draft) {
        return (
            <Card>
                <CardBody className="space-y-4">
                    <Label>{draft.id ? "Edit set" : "New set"}</Label>
                    <Field label="Name">
                        <Input
                            value={draft.name}
                            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                            placeholder="Regression set"
                        />
                    </Field>
                    <Field label="Orchestration">
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
                            <Label>Cases</Label>
                            {draft.id && (
                                <Button size="sm" variant="ghost" disabled={busy}
                                        onClick={() => propose(draft.id!)}>
                                    <Sparkles className="h-3.5 w-3.5" />
                                    {busy ? "Thinking…" : "Suggest cases"}
                                </Button>
                            )}
                        </div>

                        {proposed && (
                            <Card className="border-accent/40">
                                <CardBody className="space-y-2">
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
                                </CardBody>
                            </Card>
                        )}

                        {draft.cases.map((c, index) => (
                            <div key={index} className="flex gap-2">
                                <Input
                                    className="w-40"
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
                            <Plus className="h-3.5 w-3.5" /> Add case
                        </Button>
                    </div>

                    <div className="flex gap-2">
                        <Button onClick={save} disabled={busy || !draft.name.trim() || !draft.orchestration_id}>
                            {busy ? "Saving…" : "Save set"}
                        </Button>
                        <Button variant="ghost" onClick={() => { setDraft(null); setProposed(null); }}>
                            Cancel
                        </Button>
                    </div>
                </CardBody>
            </Card>
        );
    }

    return (
        <div className="space-y-3">
            <div className="flex items-center justify-between">
                <Label>Golden sets</Label>
                <Button size="sm" onClick={() => setDraft({ id: undefined, name: "", orchestration_id: "", cases: [] })}>
                    <Plus className="h-3.5 w-3.5" /> New set
                </Button>
            </div>

            {suites.length === 0 ? (
                <EmptyState title="No golden sets yet">
                    A set holds the inputs still, so a change in the score is a change in
                    the workflow rather than a change in the questions.
                </EmptyState>
            ) : (
                suites.map((suite) => {
                    const open = openId === suite.id;
                    const latest = history[0];
                    return (
                        <Card key={suite.id}>
                            <CardBody className="space-y-2">
                                <div className="flex items-center gap-2">
                                    <button type="button" className="flex-1 text-left"
                                            onClick={() => setOpenId(open ? null : suite.id)}>
                                        <span className="text-sm">{suite.name}</span>
                                        <Muted className="ml-2 text-xs">
                                            {suite.estimate.cases} case{suite.estimate.cases === 1 ? "" : "s"}
                                        </Muted>
                                    </button>
                                    {suite.running ? (
                                        <Button size="sm" variant="ghost" onClick={() => stop(suite)}>
                                            <Square className="h-3.5 w-3.5" /> Stop
                                        </Button>
                                    ) : (
                                        <Button size="sm" variant="ghost" onClick={() => start(suite)}
                                                disabled={suite.estimate.cases === 0}>
                                            <Play className="h-3.5 w-3.5" /> Run
                                        </Button>
                                    )}
                                    <Button size="sm" variant="ghost"
                                            onClick={() => setDraft({ ...suite })}>
                                        Edit
                                    </Button>
                                </div>

                                {open && (
                                    <div className="space-y-1 pt-1">
                                        {open && suite.running && latest && (
                                            <Muted className="block text-xs">
                                                Running — {latest.scored_count} of {latest.case_count} scored.
                                            </Muted>
                                        )}
                                        {history.length === 0 ? (
                                            <Muted className="block text-xs">Never run.</Muted>
                                        ) : (
                                            history.map((run) => (
                                                <div key={run.id} className="flex items-center gap-2 text-xs">
                                                    <Badge size="sm"
                                                           tone={run.mean_score === null ? "neutral" : "accent"}>
                                                        {run.mean_score === null ? "—" : run.mean_score.toFixed(1)}
                                                    </Badge>
                                                    <span className="text-text-muted">
                                                        {run.scored_count}/{run.case_count} scored
                                                    </span>
                                                    <Badge size="sm">v{run.rubric_version}</Badge>
                                                    {run.status !== "completed" && (
                                                        <Badge size="sm" tone="warning">{run.status}</Badge>
                                                    )}
                                                    <Muted className="ml-auto">
                                                        {run.started_at.slice(0, 16).replace("T", " ")}
                                                    </Muted>
                                                </div>
                                            ))
                                        )}
                                    </div>
                                )}
                            </CardBody>
                        </Card>
                    );
                })
            )}
        </div>
    );
}


/**
 * The step the judge liked least, when it said something useful about any.
 *
 * Only shown when there is more than one step with a score: naming the weakest
 * of one is just repeating the run's own number in smaller text, and naming a
 * weakest step that scored as well as every other one implies a problem the
 * judge did not report.
 */
function weakestStep(score: Score): { id: string; score: number; reasoning: string } | null {
    const entries = Object.entries(score.per_step ?? {});
    if (entries.length < 2) return null;
    const sorted = [...entries].sort((a, b) => a[1].score - b[1].score);
    const [id, worst] = sorted[0];
    if (worst.score >= sorted[sorted.length - 1][1].score) return null;
    return { id, score: worst.score, reasoning: worst.reasoning };
}


function TrendView({
    trend,
    scaleMax,
    onRate,
}: {
    trend: Trend;
    scaleMax: number;
    /** A person disagreeing with the judge is the only ground truth there is —
     *  the agreement figure above is the join of these and the scores. */
    onRate: (runId: string, verdict: number) => void | Promise<void>;
}) {
    const scores = trend.scores;
    const latest = scores[scores.length - 1];
    const mean = scores.reduce((sum, s) => sum + s.score, 0) / scores.length;

    return (
        <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-3">
                <Stat
                    label="Latest"
                    value={latest.score.toFixed(1)}
                    tone={scoreTone(latest.score, scaleMax)}
                    hint={latest.regression ? `Flagged — ${latest.rule}` : "Within expectation"}
                />
                <Stat label="Average" value={mean.toFixed(1)} hint={`over ${scores.length} runs`} />
                <Stat
                    label="Judge vs people"
                    value={
                        trend.agreement.rate === null
                            ? "—"
                            : `${Math.round(trend.agreement.rate * 100)}%`
                    }
                    hint={
                        trend.agreement.compared === 0
                            ? "no runs rated yet"
                            : `${trend.agreement.agreed} of ${trend.agreement.compared} agreed`
                    }
                />
            </div>

            <Sparkline scores={scores} scaleMax={scaleMax} />

            <div className="space-y-2">
                <div className="flex items-center justify-between">
                    <Label>Runs</Label>
                    {scores.length > RUNS_SHOWN && (
                        <Muted className="text-xs">
                            newest {RUNS_SHOWN} of {scores.length}
                        </Muted>
                    )}
                </div>
                {[...scores].reverse().slice(0, RUNS_SHOWN).map((score) => {
                    const thumbs = trend.feedback[score.run_id] ?? [];
                    const net = thumbs.reduce((sum, t) => sum + t.verdict, 0);
                    return (
                        <Card key={score.id}>
                            <CardBody className="space-y-1 py-2">
                                <div className="flex items-center gap-2">
                                    <Badge tone={scoreTone(score.score, scaleMax)}>
                                        {score.score.toFixed(1)}
                                    </Badge>
                                    {score.regression && (
                                        <Badge tone="danger" size="sm">
                                            {score.rule === "floor" ? "below floor" : "dropped"}
                                        </Badge>
                                    )}
                                    <Badge size="sm">v{score.rubric_version}</Badge>
                                    <div className="ml-auto flex items-center gap-1">
                                        <IconButton
                                            label="This run was good"
                                            icon={ThumbsUp}
                                            size="sm"
                                            className={net > 0 ? "text-success" : undefined}
                                            onClick={() => onRate(score.run_id, net > 0 ? 0 : 1)}
                                        />
                                        <IconButton
                                            label="This run was not good"
                                            icon={ThumbsDown}
                                            size="sm"
                                            tone={net < 0 ? "danger" : "default"}
                                            onClick={() => onRate(score.run_id, net < 0 ? 0 : -1)}
                                        />
                                        <Muted className="text-xs">{score.judged_at.slice(0, 16).replace("T", " ")}</Muted>
                                    </div>
                                </div>
                                {score.reasoning && (
                                    <Muted className="block text-xs">{score.reasoning}</Muted>
                                )}
                                {weakestStep(score) && (
                                    <Muted className="block text-xs">
                                        Weakest step: {weakestStep(score)!.id} ({weakestStep(score)!.score.toFixed(1)})
                                        {weakestStep(score)!.reasoning ? ` — ${weakestStep(score)!.reasoning}` : ""}
                                    </Muted>
                                )}
                            </CardBody>
                        </Card>
                    );
                })}
            </div>
        </div>
    );
}

/**
 * The trend, drawn as one polyline per rubric version.
 *
 * Separate polylines rather than one is the whole point: a line drawn straight
 * through a version change asserts that the two sides are comparable, which is
 * exactly what a version change means they are not.
 */
function Sparkline({ scores, scaleMax }: { scores: Score[]; scaleMax: number }) {
    const width = 640;
    const height = 96;
    // Inset so a point at either end is drawn whole rather than half outside
    // the viewBox. The newest score sits at the right edge and is the one
    // people look at first, so clipping it is the worst place to save 6px.
    const padX = 8;
    const padY = 6;
    const span = width - padX * 2;
    const step = scores.length > 1 ? span / (scores.length - 1) : 0;
    const x = (index: number) => padX + index * step;
    const y = (score: number) =>
        height - padY - (score / (scaleMax || 10)) * (height - padY * 2);

    type Segment = { version: number; points: { x: number; y: number }[] };
    const segments: Segment[] = [];
    scores.forEach((score, index) => {
        const last = segments[segments.length - 1];
        const point = { x: x(index), y: y(score.score) };
        if (!last || last.version !== score.rubric_version) {
            segments.push({ version: score.rubric_version, points: [point] });
        } else {
            last.points.push(point);
        }
    });

    return (
        <Card>
            <CardBody>
                <svg
                    viewBox={`0 0 ${width} ${height}`}
                    className="h-24 w-full overflow-visible"
                    role="img"
                    aria-label={`Score trend over ${scores.length} runs, most recent ${scores[scores.length - 1].score.toFixed(1)}`}
                >
                    {segments.map((segment, index) => (
                        <polyline
                            key={index}
                            points={segment.points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")}
                            fill="none"
                            stroke="currentColor"
                            strokeWidth={1.5}
                            strokeLinecap="round"
                            className="text-accent"
                        />
                    ))}
                    {scores.map((score, index) => (
                        // Ordinary points shrink as the window fills so the line
                        // stays a line rather than becoming a row of touching
                        // dots. Flagged ones never shrink below a size you can
                        // aim at — they are the reason to look at this chart.
                        <circle
                            key={score.id}
                            cx={x(index)}
                            cy={y(score.score)}
                            r={score.regression ? 3.5 : Math.max(0.8, Math.min(2.5, step / 4))}
                            className={score.regression ? "fill-danger" : "fill-accent"}
                        >
                            <title>
                                {`${score.score.toFixed(1)} — rubric v${score.rubric_version}${
                                    score.regression ? ` — flagged (${score.rule})` : ""
                                }`}
                            </title>
                        </circle>
                    ))}
                </svg>
                {segments.length > 1 && (
                    <Muted className="mt-1 block text-xs">
                        The line breaks where the rubric version changed — scores either side
                        answer different questions and are not comparable.
                    </Muted>
                )}
            </CardBody>
        </Card>
    );
}
