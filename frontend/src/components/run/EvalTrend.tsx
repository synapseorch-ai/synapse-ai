'use client';

/*
 * A rubric's scores for one workflow (M2h), the same in both products: the
 * headline figures as one strip, the trend, each criterion's average, then the
 * scored runs with the judge's reasons and a person's thumbs up or down.
 *
 * Colour only for meaning (D202): a score is plain text; a flagged run is red;
 * the trend is the machine's line with flagged points in red.
 */
import { ThumbsDown, ThumbsUp } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { BarList, IconButton, StatStrip } from '@/components/ui';
import { cn } from '@/lib/cn';

import {
    average, byCriterion, flagWords, latest, passRate, RUNS_SHOWN, scoreText, weakestStep,
    type EvalScore, type EvalTrendData,
} from './evalStats';
import { useClock, When } from './When';

const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function EvalTrend({
    trend, scaleMax, onRate, className,
}: {
    trend: EvalTrendData;
    scaleMax: number;
    /** A person agreeing or not with the judge: 1, -1, or 0 to clear. */
    onRate: (runId: string, verdict: number) => void | Promise<void>;
    className?: string;
}) {
    const clock = useClock();
    const scores = trend.scores;
    const last = latest(scores);
    const mean = average(scores);
    const pass = passRate(scores);
    const criteria = byCriterion(scores, trend.rubric);
    if (!last) return null;

    return (
        <div className={cn('grid gap-6', className)}>
            <div data-eval-strip>
                <StatStrip
                    size="sm"
                    label="Scores"
                    cells={[
                        {
                            label: 'Latest',
                            value: scoreText(last.score),
                            sub: last.regression ? `Flagged — ${last.rule}` : 'Within expectation',
                            dot: last.regression ? 'danger' : undefined,
                        },
                        { label: 'Average', value: scoreText(mean), sub: `over ${count(scores.length, 'run')}` },
                        {
                            label: 'Passed',
                            value: pass.rate === null ? '—' : `${Math.round(pass.rate * 100)}%`,
                            sub: `${pass.passed} of ${count(pass.of, 'run')} not flagged`,
                        },
                        {
                            label: 'Judge vs people',
                            value: trend.agreement.rate === null ? '—' : `${Math.round(trend.agreement.rate * 100)}%`,
                            sub: trend.agreement.compared === 0
                                ? 'no runs rated yet'
                                : `${trend.agreement.agreed} of ${trend.agreement.compared} agreed`,
                        },
                    ]}
                />
            </div>

            <ScoreTrend scores={scores} scaleMax={scaleMax} floor={trend.rubric?.floor ?? null} />

            {criteria.length > 0 && (
                <section data-eval-criteria aria-labelledby="eval-criteria" className="grid gap-1">
                    <h3 id="eval-criteria" className="m-0 text-[13px] font-semibold text-text">By criterion</h3>
                    <p className="m-0 text-xs text-text-faint">
                        Each criterion’s average under version {trend.rubric?.version}, out of {scaleMax}.
                    </p>
                    <BarList
                        label="Average score by criterion"
                        max={scaleMax}
                        rows={criteria.map((c) => ({
                            key: c.key,
                            label: c.name,
                            sub: count(c.runs, 'run'),
                            value: c.average,
                            display: scoreText(c.average),
                        }))}
                    />
                </section>
            )}

            <section aria-labelledby="eval-runs" className="grid gap-1">
                <div className="flex items-baseline justify-between gap-3">
                    <h3 id="eval-runs" className="m-0 text-[13px] font-semibold text-text">Runs</h3>
                    {scores.length > RUNS_SHOWN && (
                        <span className="text-xs text-text-faint">newest {RUNS_SHOWN} of {scores.length}</span>
                    )}
                </div>
                <ul aria-label="Scored runs" className="m-0 list-none border-t border-border p-0">
                    {[...scores].reverse().slice(0, RUNS_SHOWN).map((score) => (
                        <ScoreRow
                            key={score.id}
                            score={score}
                            net={(trend.feedback[score.run_id] ?? []).reduce((sum, t) => sum + t.verdict, 0)}
                            onRate={onRate}
                            clock={clock}
                        />
                    ))}
                </ul>
            </section>
        </div>
    );
}

function ScoreRow({
    score, net, onRate, clock,
}: {
    score: EvalScore;
    net: number;
    onRate: (runId: string, verdict: number) => void | Promise<void>;
    clock: ReturnType<typeof useClock>;
}) {
    const weakest = weakestStep(score);
    return (
        <li data-score-row={score.run_id} className="grid gap-1 border-b border-border px-1 py-2.5">
            <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
                <span className={cn('text-sm font-semibold tabular-nums', score.regression ? 'text-danger' : 'text-text')}>
                    {scoreText(score.score)}
                </span>
                {score.regression && (
                    <span className="text-xs font-medium text-danger">{flagWords(score.rule)}</span>
                )}
                <span className="rounded bg-surface-2 px-1.5 text-[11.5px] font-medium tabular-nums text-text-muted">
                    v{score.rubric_version}
                </span>
                <span className="ml-auto flex items-center gap-1">
                    <IconButton
                        label="This run was good"
                        icon={ThumbsUp}
                        size="sm"
                        aria-pressed={net > 0}
                        className={net > 0 ? 'text-success' : undefined}
                        onClick={() => onRate(score.run_id, net > 0 ? 0 : 1)}
                    />
                    <IconButton
                        label="This run was not good"
                        icon={ThumbsDown}
                        size="sm"
                        aria-pressed={net < 0}
                        tone={net < 0 ? 'danger' : 'default'}
                        onClick={() => onRate(score.run_id, net < 0 ? 0 : -1)}
                    />
                    <When iso={score.judged_at} clock={clock} className="whitespace-nowrap text-xs tabular-nums text-text-faint" />
                </span>
            </div>
            {score.reasoning && <p className="m-0 text-[13px] text-text-muted">{score.reasoning}</p>}
            {weakest && (
                <p className="m-0 text-xs text-text-faint">
                    Weakest step: {weakest.id} ({weakest.score.toFixed(1)})
                    {weakest.reasoning ? ` — ${weakest.reasoning}` : ''}
                </p>
            )}
        </li>
    );
}

/**
 * The trend, one line per rubric version: a line drawn straight through a
 * version change would say the two sides are comparable, which is what a
 * version change means they are not. The floor, when set, is a faint rule.
 */
export function ScoreTrend({
    scores, scaleMax, floor,
}: {
    scores: EvalScore[];
    scaleMax: number;
    floor?: number | null;
}) {
    // Drawn at the width it is shown at, so the points stay round at any width.
    const box = useRef<HTMLElement>(null);
    const [width, setWidth] = useState(640);
    useEffect(() => {
        const el = box.current;
        if (!el || typeof ResizeObserver === 'undefined') return;
        const watch = new ResizeObserver(([entry]) => {
            const w = Math.round(entry.contentRect.width);
            if (w > 0) setWidth(w);
        });
        watch.observe(el);
        return () => watch.disconnect();
    }, []);
    const height = 112;
    // Inset so a point at either end is drawn whole.
    const padX = 8;
    const padY = 8;
    const span = width - padX * 2;
    const step = scores.length > 1 ? span / (scores.length - 1) : 0;
    const x = (index: number) => padX + index * step;
    const y = (value: number) => height - padY - (value / (scaleMax || 10)) * (height - padY * 2);

    type Segment = { version: number; points: { x: number; y: number }[] };
    const segments: Segment[] = [];
    scores.forEach((score, index) => {
        const last = segments[segments.length - 1];
        const point = { x: x(index), y: y(score.score) };
        if (!last || last.version !== score.rubric_version) segments.push({ version: score.rubric_version, points: [point] });
        else last.points.push(point);
    });
    const last = scores[scores.length - 1];
    if (!last) return null;

    return (
        <figure ref={box} data-eval-trend className="m-0 grid gap-1.5">
            <svg
                viewBox={`0 0 ${width} ${height}`}
                className="h-28 w-full overflow-visible"
                role="img"
                aria-label={`Score trend over ${count(scores.length, 'run')}, most recent ${last.score.toFixed(1)}`}
            >
                <line x1={padX} x2={width - padX} y1={y(0)} y2={y(0)} className="stroke-border" strokeWidth={1} vectorEffect="non-scaling-stroke" />
                {floor !== null && floor !== undefined && (
                    <line
                        x1={padX} x2={width - padX} y1={y(floor)} y2={y(floor)}
                        className="stroke-text-faint" strokeWidth={1} strokeDasharray="3 4" vectorEffect="non-scaling-stroke"
                    >
                        <title>{`Floor ${floor}`}</title>
                    </line>
                )}
                {segments.map((segment, index) => (
                    <polyline
                        key={index}
                        points={segment.points.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}
                        fill="none"
                        stroke="currentColor"
                        strokeWidth={1.75}
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        vectorEffect="non-scaling-stroke"
                        className="text-machine"
                    />
                ))}
                {scores.map((score, index) => (
                    // Ordinary points shrink as the window fills; flagged ones stay big enough to aim at.
                    <circle
                        key={score.id}
                        cx={x(index)}
                        cy={y(score.score)}
                        r={score.regression ? 3.5 : Math.max(1, Math.min(2.5, step / 4))}
                        className={score.regression ? 'fill-danger' : 'fill-machine'}
                    >
                        <title>
                            {`${score.score.toFixed(1)} — rubric v${score.rubric_version}${score.regression ? ` — flagged (${score.rule})` : ''}`}
                        </title>
                    </circle>
                ))}
            </svg>
            <figcaption className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-faint">
                <span>Oldest on the left, newest on the right, out of {scaleMax}.</span>
                {floor !== null && floor !== undefined && <span>The dashed line is the floor ({floor}).</span>}
                {segments.length > 1 && (
                    <span>
                        The line breaks where the rubric version changed — scores either side answer different
                        questions and are not comparable.
                    </span>
                )}
            </figcaption>
        </figure>
    );
}
