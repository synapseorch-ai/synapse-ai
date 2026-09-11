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
import { Plus, Target, ThumbsDown, ThumbsUp, Trash } from "lucide-react";

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
                            <TrendView trend={trend} scaleMax={scaleMax} />
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

function TrendView({ trend, scaleMax }: { trend: Trend; scaleMax: number }) {
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
                <Label>Runs</Label>
                {[...scores].reverse().map((score) => {
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
                                    {net !== 0 &&
                                        (net > 0 ? (
                                            <ThumbsUp className="h-3.5 w-3.5 text-success" />
                                        ) : (
                                            <ThumbsDown className="h-3.5 w-3.5 text-danger" />
                                        ))}
                                    <Muted className="ml-auto text-xs">{score.judged_at.slice(0, 16).replace("T", " ")}</Muted>
                                </div>
                                {score.reasoning && (
                                    <Muted className="block text-xs">{score.reasoning}</Muted>
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
                        <circle
                            key={score.id}
                            cx={x(index)}
                            cy={y(score.score)}
                            r={score.regression ? 4 : 2.5}
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
