/*
 * Evals' figures (M2h), pure so both products and their tests share them. The
 * judge scores a finished run against a rubric; these read what it said.
 *
 * Scores either side of a rubric version are answers to different questions,
 * so the per-criterion averages use the current version only, and the trend
 * breaks its line where the version changes (`ScoreTrend`).
 */

export type EvalCriterion = { id?: string; name: string };

/** As both products' `/evals/orchestrations/{id}` return it (only what the screen reads). */
export type EvalScore = {
    id: number | string;
    run_id: string;
    rubric_version: number;
    score: number;
    per_criterion?: Record<string, { score: number; reasoning?: string }>;
    per_step?: Record<string, { score: number; reasoning?: string }>;
    reasoning?: string;
    judged_at: string;
    regression: boolean;
    rule: string | null;
};

export type EvalTrendData = {
    scores: EvalScore[];
    feedback: Record<string, { verdict: number }[]>;
    rubric: { version: number; criteria: EvalCriterion[]; scale?: { min: number; max: number }; floor?: number | null } | null;
    agreement: { compared: number; agreed: number; rate: number | null };
};

/** How many scored runs the list shows; the chart carries the whole window. */
export const RUNS_SHOWN = 20;

export function latest(scores: EvalScore[]): EvalScore | null {
    return scores.length ? scores[scores.length - 1] : null;
}

export function average(scores: EvalScore[]): number | null {
    if (!scores.length) return null;
    return scores.reduce((sum, s) => sum + s.score, 0) / scores.length;
}

/** The share of runs the rubric did not flag (below its floor, or a drop against recent runs). */
export function passRate(scores: EvalScore[]): { passed: number; of: number; rate: number | null } {
    const passed = scores.filter((s) => !s.regression).length;
    return { passed, of: scores.length, rate: scores.length ? passed / scores.length : null };
}

/** Each criterion's average under the rubric's current version, named from the rubric. */
export function byCriterion(
    scores: EvalScore[],
    rubric: EvalTrendData['rubric'],
): { key: string; name: string; average: number; runs: number }[] {
    if (!rubric) return [];
    const current = scores.filter((s) => s.rubric_version === rubric.version);
    return rubric.criteria
        .map((c, index) => {
            const key = c.id ?? c.name ?? String(index);
            const values = current
                .map((s) => s.per_criterion?.[key]?.score ?? (c.name ? s.per_criterion?.[c.name]?.score : undefined))
                .filter((v): v is number => typeof v === 'number');
            return {
                key,
                name: c.name || `Criterion ${index + 1}`,
                average: values.length ? values.reduce((a, b) => a + b, 0) / values.length : NaN,
                runs: values.length,
            };
        })
        .filter((c) => c.runs > 0);
}

/** "below floor" or "dropped": why a run was flagged. */
export function flagWords(rule: string | null | undefined): string {
    return rule === 'floor' ? 'below floor' : 'dropped';
}

/** The step the judge liked least, when it said something useful about any. */
export function weakestStep(score: EvalScore): { id: string; score: number; reasoning: string } | null {
    const entries = Object.entries(score.per_step ?? {});
    if (entries.length < 2) return null;
    const sorted = [...entries].sort((a, b) => a[1].score - b[1].score);
    const [id, worst] = sorted[0];
    if (worst.score >= sorted[sorted.length - 1][1].score) return null;
    return { id, score: worst.score, reasoning: worst.reasoning ?? '' };
}

/** A score on its scale: "8.5", or "8.5 / 10" when asked for the scale too. */
export function scoreText(score: number | null | undefined, max?: number): string {
    if (score === null || score === undefined || Number.isNaN(score)) return '—';
    return max ? `${score.toFixed(1)} / ${max}` : score.toFixed(1);
}
