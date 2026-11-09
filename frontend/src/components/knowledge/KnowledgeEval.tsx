'use client';

/*
 * Evaluate: test a knowledge base on its own documents (V1, D168).
 *
 * A question set is a list of questions, each with where its answer is written
 * (a quote in a document). Questions come from drafts — written by a model from
 * the base's own passages, and saved only when a person adds them — or from the
 * base's real searches, promoted with the passages that answer them.
 *
 * A run searches every question once per snapshot and scores where the answer
 * ranked, with no judge: keyword only, meaning only, both fused, and as the base
 * serves it. Two snapshots side by side compare an index built two ways. "Try
 * weights" fuses the same searches again for $0. A run can also grade a plain
 * answer drawn from the passages, with a judge from another model family (paid,
 * priced first). After a settings change the judge-free part re-runs by itself.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ClipboardCheck, ListPlus, Pencil, Play, Search, Sparkles, Square, Trash2 } from 'lucide-react';

import {
    Badge, Button, Checkbox, Combobox, ConfirmDialog, EmptyState, ErrorNote, Field, IconButton, Input, Label, Modal,
    Muted, Section, Select, Spinner, TBody, TD, TH, THead, TR, Table, Textarea, type ComboboxOption,
} from '@/components/ui';
import { RetrievalTrace } from '@/components/RetrievalTrace';
import { count } from '@/lib/copy';
import { cn } from '@/lib/cn';
import {
    DRAFT_DROPPED, EVAL_KINDS, formatUsd, getQueryTrace, inBase, knowledgeApi, servedHit5,
    type EvalCase, type EvalCaseKind, type EvalConfigResult, type EvalDraft, type EvalEstimate,
    type EvalQuery, type EvalRun, type EvalRunRequest, type EvalRunSummary, type EvalScores, type EvalSet,
    type EvalSetSummary, type KnowledgeBase, type SearchTrace,
} from '@/lib/knowledge';

import { useKnowledgeHost, type KnowledgeWorkspace } from './host';

type Notify = (message: string, tone?: 'success' | 'warning' | 'danger') => void;

const ACTIVE = new Set(['queued', 'running', 'cancelling']);
const RUN_STATUS: Record<string, string> = {
    queued: 'Waiting to start', running: 'Running', cancelling: 'Stopping', completed: 'Done', cancelled: 'Stopped',
    failed: 'Failed', interrupted: 'Interrupted', skipped: 'Skipped',
};
const KIND_FILTER: { value: 'all' | EvalCaseKind; label: string }[] = [
    { value: 'all', label: 'All questions' },
    ...(Object.entries(EVAL_KINDS) as [EvalCaseKind, string][]).map(([value, label]) => ({ value, label })),
];

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
const pct = (n: number | undefined) => (n == null ? '—' : `${Math.round(n * 100)}%`);
const dec = (n: number | undefined) => (n == null ? '—' : n.toFixed(2));
const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : '—');
const shortSnap = (id: string) => `…${id.slice(-6)}`;
const SNAP_STATUS: Record<string, string> = { active: 'Serving', building: 'Being built', retired: 'Replaced' };

/** The workspace's models and the default a draft or a judge starts from. */
function useWorkspaceModels() {
    const { loadWorkspace } = useKnowledgeHost();
    const [state, setState] = useState<KnowledgeWorkspace | null>(null);
    useEffect(() => {
        let live = true;
        loadWorkspace().then(w => { if (live) setState(w); });
        return () => { live = false; };
    }, [loadWorkspace]);
    return state;
}

/** The estimate's lines as one sentence, saying what is unpriced and what counts toward the budget. */
function EstimateLine({ estimate, error, busy }: { estimate: EvalEstimate | null; error: string; busy: boolean }) {
    if (error) return <ErrorNote>{error}</ErrorNote>;
    if (!estimate) return <Muted className="text-xs">{busy ? 'Pricing…' : ' '}</Muted>;
    const parts = estimate.lines.map(l => `${l.part === 'search' ? 'searches' : l.part}${l.model ? ` (${l.model})` : ''}: ${l.est_usd == null ? 'price unknown' : formatUsd(l.est_usd)}`);
    return (
        <div className="space-y-1 text-xs text-text-muted" aria-live="polite">
            <div><span className="text-text">About {formatUsd(estimate.total_est_usd)}</span> — {parts.join(' · ')}</div>
            {estimate.unpriced.length > 0 && (
                <div>No price is known for {estimate.unpriced.join(', ')}: its calls are not in the total.</div>
            )}
            {estimate.lines.some(l => l.counts_toward_budget) && estimate.budget_remaining_usd != null && (
                <div>Searches count toward this base&apos;s budget: {formatUsd(estimate.budget_remaining_usd)} is left this month.</div>
            )}
        </div>
    );
}

/** An estimate that follows `body` as it is edited. */
function useEstimate(kbId: string, body: Parameters<typeof knowledgeApi.eval.estimate>[1] | null) {
    const [estimate, setEstimate] = useState<EvalEstimate | null>(null);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const key = JSON.stringify(body);
    useEffect(() => {
        if (!body) return;
        const ctrl = new AbortController();
        setBusy(true);
        const timer = setTimeout(() => {
            knowledgeApi.eval.estimate(kbId, body, { signal: ctrl.signal })
                .then(e => { setEstimate(e); setError(''); })
                .catch(e => { if (!ctrl.signal.aborted) { setEstimate(null); setError(message(e)); } })
                .finally(() => { if (!ctrl.signal.aborted) setBusy(false); });
        }, 300);
        return () => { clearTimeout(timer); ctrl.abort(); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [kbId, key]);
    return { estimate, error, busy };
}

// ── the section ───────────────────────────────────────────────────────────────

export function Evaluate({ kb, notify, onScore }: {
    kb: KnowledgeBase;
    notify: Notify;
    /** The served index's top-5 rate from the selected set's latest run, or null: the base's "Finds the answer" (M2e). */
    onScore?: (hit5: number | null) => void;
}) {
    const { canEdit } = useKnowledgeHost();
    const [sets, setSets] = useState<EvalSetSummary[] | null>(null);
    const [setId, setSetId] = useState<string | null>(null);
    const [current, setCurrent] = useState<EvalSet | null>(null);
    const [runs, setRuns] = useState<EvalRunSummary[]>([]);
    const [runId, setRunId] = useState<string | null>(null);
    const [run, setRun] = useState<EvalRun | null>(null);
    const [error, setError] = useState('');
    const [suggestions, setSuggestions] = useState<EvalDraft[]>([]);
    const [dialog, setDialog] = useState<'draft' | 'promote' | 'run' | 'delete' | null>(null);
    const [editing, setEditing] = useState<EvalCase | null>(null);
    const [busy, setBusy] = useState(false);

    const loadSets = useCallback(async (pick?: string) => {
        try {
            const list = await knowledgeApi.eval.sets(kb.id);
            setSets(list);
            setSetId(prev => pick ?? (prev && list.some(s => s.id === prev) ? prev : list[0]?.id ?? null));
            setError('');
        } catch (e) {
            setError(message(e));
        }
    }, [kb.id]);

    useEffect(() => { void loadSets(); }, [loadSets]);

    const loadSet = useCallback(async () => {
        if (!setId) { setCurrent(null); setRuns([]); return; }
        try {
            const [s, r] = await Promise.all([knowledgeApi.eval.set(kb.id, setId), knowledgeApi.eval.runs(kb.id, setId)]);
            setCurrent(s);
            setRuns(r);
            setRunId(prev => (prev && r.some(x => x.id === prev) ? prev : r.find(x => x.status !== 'skipped')?.id ?? r[0]?.id ?? null));
        } catch (e) {
            setError(message(e));
        }
    }, [kb.id, setId]);

    useEffect(() => { void loadSet(); }, [loadSet]);

    const active = !!run && run.id === runId && ACTIVE.has(run.status);
    const wasActive = useRef(false);
    useEffect(() => {
        if (!runId) { setRun(null); return; }
        let live = true;
        const load = () => knowledgeApi.eval.run(kb.id, runId)
            .then(r => {
                if (!live) return;
                setRun(r);
                setRuns(prev => prev.map(x => (x.id === r.id ? r : x)));
                if (wasActive.current && !ACTIVE.has(r.status)) {  // it just finished: the set's last run moved
                    void loadSet();
                    void loadSets(setId ?? undefined);
                }
                wasActive.current = ACTIVE.has(r.status);
            })
            .catch(e => { if (live) setError(message(e)); });
        void load();
        const timer = active ? setInterval(load, 2000) : null;
        return () => { live = false; if (timer) clearInterval(timer); };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [kb.id, runId, active]);

    const save = async (cases: EvalCase[]) => {
        if (!current) return;
        setBusy(true);
        try {
            setCurrent(await knowledgeApi.eval.saveSet(kb.id, current.id, { revision: current.revision, cases }));
            void loadSets(current.id);
        } catch (e) {
            notify(message(e), 'danger');
            void loadSet();
        } finally {
            setBusy(false);
        }
    };

    const newSet = async () => {
        setBusy(true);
        try {
            const made = await knowledgeApi.eval.createSet(kb.id, { name: `Questions ${(sets?.length ?? 0) + 1}` });
            await loadSets(made.id);
            setDialog('draft');
        } catch (e) {
            notify(message(e), 'danger');
        } finally {
            setBusy(false);
        }
    };

    const latest = sets?.find(s => s.id === setId);
    const hit5 = latest ? servedHit5(latest) : null;
    useEffect(() => { onScore?.(hit5); }, [hit5, onScore]);
    const summary = hit5 != null
        ? <Muted>Answer found in the top 5 for {pct(hit5)} of questions</Muted>
        : sets?.length ? <Muted>{count(sets.length, 'question set')}</Muted> : undefined;

    return (
        <Section
            variant="plain"
            title="Evaluate"
            description="Test how well this knowledge base finds the answers to questions about its own documents."
            summary={summary}
        >
            {error && <ErrorNote className="mb-3">{error}</ErrorNote>}
            {sets === null ? (
                <div className="flex justify-center py-4"><Spinner /></div>
            ) : sets.length === 0 ? (
                <EmptyState
                    title="No question sets yet"
                    action={canEdit && <Button onClick={newSet} disabled={busy}><ListPlus className="size-4" aria-hidden />New question set</Button>}
                >
                    Draft questions from this knowledge base&apos;s own passages, or keep real searches with the
                    passages that answer them. Then see how often each kind of search finds the answer.
                </EmptyState>
            ) : (
                <div className="space-y-4">
                    <div className="flex flex-wrap items-center gap-2">
                        {sets.length > 1 && (
                            <Select size="sm" className="w-auto max-w-xs" value={setId ?? ''} onChange={v => { setSetId(v); setSuggestions([]); setRunId(null); }}
                                options={sets.map(s => ({ value: s.id, label: `${s.name} (${s.case_count})` }))} aria-label="Question set" />
                        )}
                        {sets.length === 1 && <Label>{current?.name ?? sets[0].name}</Label>}
                        {canEdit && <div className="ml-auto flex flex-wrap gap-2">
                            <Button size="sm" variant="secondary" onClick={() => setDialog('draft')} disabled={!current}>
                                <Sparkles className="size-4" aria-hidden />Draft questions
                            </Button>
                            <Button size="sm" variant="secondary" onClick={() => setDialog('promote')} disabled={!current}>
                                <Search className="size-4" aria-hidden />From recent searches
                            </Button>
                            <Button size="sm" onClick={() => setDialog('run')} disabled={!current?.cases.length || !!active}>
                                <Play className="size-4" aria-hidden />Run
                            </Button>
                            <Button size="sm" variant="ghost" onClick={newSet} disabled={busy}>
                                <ListPlus className="size-4" aria-hidden />New set
                            </Button>
                            <IconButton label={`Delete ${current?.name ?? 'question set'}`} onClick={() => setDialog('delete')} disabled={!current} icon={Trash2} />
                        </div>}
                    </div>

                    {suggestions.length > 0 && current && (
                        <Suggestions
                            kb={kb}
                            drafts={suggestions}
                            busy={busy}
                            onChange={setSuggestions}
                            onAdd={async picked => {
                                await save([...current.cases, ...picked.map(d => ({ ...d, reference_answer: d.reference_answer ?? '' }))]);
                                setSuggestions(prev => prev.filter(d => !picked.includes(d)));
                            }}
                        />
                    )}

                    {current && (
                        <Cases kb={kb} cases={current.cases} busy={busy}
                            onEdit={canEdit ? setEditing : undefined}
                            onRemove={canEdit ? c => save(current.cases.filter(x => x.id !== c.id)) : undefined} />
                    )}

                    {runs.length > 0 && (
                        <Results kb={kb} runs={runs} run={run} runId={runId} onPick={setRunId} notify={notify}
                            onCancel={canEdit ? async () => {
                                if (!run) return;
                                try { setRun({ ...run, ...(await knowledgeApi.eval.cancelRun(kb.id, run.id)) }); }
                                catch (e) { notify(message(e), 'danger'); }
                            } : undefined} />
                    )}
                </div>
            )}

            {current && dialog === 'draft' && (
                <DraftDialog kb={kb} setId={current.id} onClose={() => setDialog(null)}
                    onDrafted={(drafts, dropped) => {
                        setSuggestions(prev => [...prev, ...drafts]);
                        const lost = Object.entries(dropped);
                        if (lost.length) {
                            notify(`${count(lost.reduce((a, [, n]) => a + n, 0), 'draft')} left out: ${lost.map(([why, n]) => `${n} because ${DRAFT_DROPPED[why] ?? why}`).join('; ')}.`, 'warning');
                        }
                    }} />
            )}
            {current && dialog === 'promote' && (
                <PromoteDialog kb={kb} set={current} onClose={() => setDialog(null)}
                    onPromoted={s => { setCurrent(s); void loadSets(s.id); notify('Added the search to the set.', 'success'); }} />
            )}
            {current && dialog === 'run' && (
                <RunDialog kb={kb} set={current} previous={runs} onClose={() => setDialog(null)}
                    onStarted={r => { wasActive.current = true; setRuns(prev => [r, ...prev]); setRunId(r.id); setRun(null); }} />
            )}
            {editing && current && (
                <EditCase kase={editing} onClose={() => setEditing(null)}
                    onSave={edited => { setEditing(null); void save(current.cases.map(c => (c.id === edited.id ? edited : c))); }} />
            )}
            <ConfirmDialog
                open={dialog === 'delete'}
                onClose={() => setDialog(null)}
                title={`Delete ${current?.name ?? 'this question set'}?`}
                description="Its questions and every run of them go. The searches it kept are released to the 30-day cleanup."
                confirmLabel="Delete"
                busy={busy}
                onConfirm={async () => {
                    if (!current) return;
                    setBusy(true);
                    try {
                        await knowledgeApi.eval.deleteSet(kb.id, current.id);
                        setDialog(null);
                        setSuggestions([]);
                        setRunId(null);
                        await loadSets();
                    } catch (e) {
                        notify(message(e), 'danger');
                    } finally {
                        setBusy(false);
                    }
                }}
            />
        </Section>
    );
}

// ── the questions ─────────────────────────────────────────────────────────────

function Evidence({ kb, kase }: { kb: KnowledgeBase; kase: Pick<EvalCase, 'evidence'> }) {
    return (
        <ul className="space-y-1">
            {kase.evidence.map((ev, i) => (
                <li key={i} className="min-w-0 text-xs text-text-muted">
                    <span className="font-mono text-text" translate="no">{inBase(ev.path, kb.folder)}</span>
                    <span className="line-clamp-2"> “{ev.quote}”</span>
                </li>
            ))}
        </ul>
    );
}

function Cases({ kb, cases, busy, onEdit, onRemove }: {
    kb: KnowledgeBase; cases: EvalCase[]; busy: boolean;
    /** Both absent for someone who may not change the set: no actions column. */
    onEdit?: (c: EvalCase) => void; onRemove?: (c: EvalCase) => void;
}) {
    if (!cases.length) return <Muted>No questions in this set yet. Draft some, or add a recent search.</Muted>;
    return (
        <Table density="compact" minWidth="32rem">
            <THead>
                <TR hover={false}>
                    <TH width="full">Question, and where its answer is</TH>
                    <TH width="min">Kind</TH>
                    {onEdit && onRemove && <TH width="min"><span className="sr-only">Actions</span></TH>}
                </TR>
            </THead>
            <TBody>
                {cases.map(c => (
                    <TR key={c.id ?? c.question}>
                        <TD>
                            <div className="space-y-1">
                                <div className="text-sm text-text">{c.question}</div>
                                <Evidence kb={kb} kase={c} />
                            </div>
                        </TD>
                        <TD nowrap>
                            <Badge size="sm">{EVAL_KINDS[c.kind]}</Badge>
                            {c.overlap != null && c.kind === 'paraphrase' && (
                                <Muted className="block text-xs">{pct(c.overlap)} of its words in the passage</Muted>
                            )}
                        </TD>
                        {onEdit && onRemove && (
                            <TD nowrap>
                                <div className="flex gap-1">
                                    <IconButton label={`Edit “${c.question.slice(0, 40)}”`} onClick={() => onEdit(c)} disabled={busy} icon={Pencil} />
                                    <IconButton label={`Remove “${c.question.slice(0, 40)}”`} onClick={() => onRemove(c)} disabled={busy} icon={Trash2} />
                                </div>
                            </TD>
                        )}
                    </TR>
                ))}
            </TBody>
        </Table>
    );
}

function EditCase({ kase, onClose, onSave }: { kase: EvalCase; onClose: () => void; onSave: (c: EvalCase) => void }) {
    const [question, setQuestion] = useState(kase.question);
    const [answer, setAnswer] = useState(kase.reference_answer ?? '');
    return (
        <Modal open onClose={onClose} title="Edit question"
            description="Where its answer is written stays as it was found."
            footer={<>
                <Button variant="ghost" onClick={onClose}>Cancel</Button>
                <Button onClick={() => onSave({ ...kase, question: question.trim(), reference_answer: answer.trim() })}
                    disabled={!question.trim()}>Save</Button>
            </>}>
            <div className="space-y-3">
                <Field label="Question"><Textarea value={question} onChange={e => setQuestion(e.target.value)} rows={3} maxLength={2000} /></Field>
                <Field label="Expected answer" hint="For people reading the results; the scores do not use it.">
                    <Textarea value={answer} onChange={e => setAnswer(e.target.value)} rows={2} maxLength={4000} />
                </Field>
            </div>
        </Modal>
    );
}

function Suggestions({ kb, drafts, busy, onChange, onAdd }: {
    kb: KnowledgeBase; drafts: EvalDraft[]; busy: boolean; onChange: (d: EvalDraft[]) => void;
    onAdd: (picked: EvalDraft[]) => Promise<void>;
}) {
    return (
        <div className="rounded-md border border-accent/40 p-3">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <Muted>Suggestions only — nothing is saved until you add them.</Muted>
                <div className="flex gap-2">
                    <Button size="sm" onClick={() => onAdd(drafts)} disabled={busy}>Add all</Button>
                    <Button size="sm" variant="ghost" onClick={() => onChange([])} disabled={busy}>Dismiss all</Button>
                </div>
            </div>
            <ul className="divide-y divide-border">
                {drafts.map((d, i) => (
                    <li key={i} className="space-y-2 py-2">
                        <div className="flex items-start gap-2">
                            <Badge size="sm" className="mt-1.5">{EVAL_KINDS[d.kind]}</Badge>
                            <Input value={d.question} aria-label={`Suggested question ${i + 1}`} maxLength={2000}
                                onChange={e => onChange(drafts.map((x, j) => (j === i ? { ...x, question: e.target.value } : x)))} />
                            <Button size="sm" variant="secondary" onClick={() => onAdd([d])} disabled={busy || !d.question.trim()}>Add</Button>
                            <IconButton label={`Dismiss suggestion ${i + 1}`} onClick={() => onChange(drafts.filter((_, j) => j !== i))} icon={Trash2} />
                        </div>
                        <Evidence kb={kb} kase={d} />
                        {d.kind === 'paraphrase' && d.overlap != null && (
                            <Muted className="text-xs">{pct(d.overlap)} of its words are the passage&apos;s own.</Muted>
                        )}
                    </li>
                ))}
            </ul>
        </div>
    );
}

// ── drafting ──────────────────────────────────────────────────────────────────

function Count({ label, value, onChange }: { label: string; value: number; onChange: (n: number) => void }) {
    return (
        <Field label={label}>
            <Input type="number" min={0} max={10} value={value} inputMode="numeric"
                onChange={e => onChange(Math.max(0, Math.min(10, Number(e.target.value) || 0)))} />
        </Field>
    );
}

function DraftDialog({ kb, setId, onClose, onDrafted }: {
    kb: KnowledgeBase; setId: string; onClose: () => void;
    onDrafted: (drafts: EvalDraft[], dropped: Record<string, number>) => void;
}) {
    const { withUnreachable } = useKnowledgeHost();
    const models = useWorkspaceModels();
    const [model, setModel] = useState('');
    const [direct, setDirect] = useState(4);
    const [paraphrase, setParaphrase] = useState(4);
    const [multiHop, setMultiHop] = useState(2);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const chosen = model || models?.judge || models?.model || '';
    const total = direct + paraphrase + multiHop;
    const body = chosen && total >= 1 && total <= 10
        ? { drafts: { model: chosen, direct, paraphrase, multi_hop: multiHop, set_id: setId } } : null;
    const priced = useEstimate(kb.id, body);
    const options: ComboboxOption[] = useMemo(
        () => withUnreachable(models?.chatModels ?? [], chosen || undefined), [withUnreachable, models, chosen]);

    const draft = async () => {
        if (!body) return;
        setBusy(true);
        setError('');
        try {
            const out = await knowledgeApi.eval.drafts(kb.id, body.drafts);
            onDrafted(out.drafts, out.dropped);
            onClose();
        } catch (e) {
            setError(message(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal open onClose={onClose} title="Draft questions" size="lg"
            description="A model writes questions from this knowledge base's own passages, and copies the words that answer each. You choose which to keep."
            footer={<>
                <Button variant="ghost" onClick={onClose}>Cancel</Button>
                <Button onClick={draft} disabled={busy || !body}>{busy ? <Spinner /> : <Sparkles className="size-4" aria-hidden />}Draft {count(total, 'question')}</Button>
            </>}>
            <div className="space-y-4">
                <Field label="Model that writes them" hint="Paid: one call per question (two for a paraphrase it has to ask again).">
                    <Combobox value={chosen || undefined} onChange={setModel} options={options} searchPlaceholder="Search models or providers…"
                        aria-label="Model that writes the questions" />
                </Field>
                <div className="grid grid-cols-3 gap-3">
                    <Count label="Direct" value={direct} onChange={setDirect} />
                    <Count label="Paraphrased" value={paraphrase} onChange={setParaphrase} />
                    <Count label="Multi-hop" value={multiHop} onChange={setMultiHop} />
                </div>
                <Muted className="block text-xs">
                    Direct questions are asked plainly; paraphrased ones in other words than the passage; multi-hop
                    ones need two documents. Up to 10 at a time.
                </Muted>
                {total > 10 && <ErrorNote>Ask for 10 questions or fewer at a time.</ErrorNote>}
                <EstimateLine {...priced} />
                {error && <ErrorNote>{error}</ErrorNote>}
            </div>
        </Modal>
    );
}

// ── promoting a real search ───────────────────────────────────────────────────

function PromoteDialog({ kb, set, onClose, onPromoted }: {
    kb: KnowledgeBase; set: EvalSet; onClose: () => void; onPromoted: (s: EvalSet) => void;
}) {
    const [queries, setQueries] = useState<EvalQuery[] | null>(null);
    const [picked, setPicked] = useState<EvalQuery | null>(null);
    const [ticked, setTicked] = useState<string[]>([]);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const kept = useMemo(() => new Set(set.cases.map(c => c.query_id).filter(Boolean)), [set.cases]);

    useEffect(() => {
        knowledgeApi.eval.queries(kb.id).then(setQueries).catch(e => setError(message(e)));
    }, [kb.id]);

    const add = async () => {
        if (!picked) return;
        setBusy(true);
        try {
            onPromoted(await knowledgeApi.eval.promote(kb.id, set.id, { query_id: picked.query_id, chunk_ids: ticked }));
            onClose();
        } catch (e) {
            setError(message(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal open onClose={onClose} title="Add a recent search" size="lg"
            description="Pick a search people made, then tick the passages that answer it. The search is kept past the 30-day cleanup."
            footer={<>
                <Button variant="ghost" onClick={picked ? () => { setPicked(null); setTicked([]); } : onClose}>{picked ? 'Back' : 'Cancel'}</Button>
                {picked && <Button onClick={add} disabled={busy || !ticked.length}>Add to set</Button>}
            </>}>
            {error && <ErrorNote className="mb-3">{error}</ErrorNote>}
            {queries === null ? <div className="flex justify-center py-4"><Spinner /></div> : !picked ? (
                queries.length === 0 ? <Muted>No searches of this knowledge base in the last 30 days.</Muted> : (
                    <ul className="max-h-96 divide-y divide-border overflow-y-auto">
                        {queries.map(q => (
                            <li key={q.query_id}>
                                <button type="button" onClick={() => setPicked(q)} disabled={kept.has(q.query_id)}
                                    className="w-full rounded px-2 py-2 text-left hover:bg-surface-2 disabled:opacity-50">
                                    <div className="text-sm text-text">{q.query || '(empty)'}</div>
                                    <Muted className="text-xs">{when(q.created_at)} · {q.surface} · {count(q.hits.length, 'passage')}{kept.has(q.query_id) ? ' · already in this set' : ''}</Muted>
                                </button>
                            </li>
                        ))}
                    </ul>
                )
            ) : (
                <div className="space-y-3">
                    <div className="text-sm text-text">{picked.query}</div>
                    <Muted className="block text-xs">Which passages answer it?</Muted>
                    <ul className="space-y-2">
                        {picked.hits.map(h => (
                            <li key={h.chunk_id} className="flex items-start gap-2">
                                <Checkbox checked={ticked.includes(h.chunk_id)} label={`${h.label} answers it`}
                                    onChange={on => setTicked(prev => (on ? [...prev, h.chunk_id].slice(0, 4) : prev.filter(c => c !== h.chunk_id)))} />
                                <div className="min-w-0 text-xs">
                                    <span className="font-mono text-text-faint" translate="no">{h.label} </span>
                                    <span className="font-mono text-text" translate="no">{inBase(h.path, kb.folder)}</span>
                                    <p className="line-clamp-2 text-text-muted">{h.snippet}</p>
                                </div>
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </Modal>
    );
}

// ── running ───────────────────────────────────────────────────────────────────

type SnapChoice = { id: string; label: string };

function RunDialog({ kb, set, previous, onClose, onStarted }: {
    kb: KnowledgeBase; set: EvalSet; previous: EvalRunSummary[]; onClose: () => void;
    onStarted: (r: EvalRunSummary) => void;
}) {
    const { withUnreachable } = useKnowledgeHost();
    const models = useWorkspaceModels();
    const choices = useMemo<SnapChoice[]>(() => {
        const out: SnapChoice[] = [];
        if (kb.state.active_snapshot) out.push({ id: kb.state.active_snapshot, label: 'The index serving now' });
        const build = kb.state.build;
        if (build?.status === 'building') {
            out.push({ id: build.snapshot_id, label: `The index being rebuilt (${build.docs_done} of ${build.docs_total} documents)` });
        }
        for (const r of previous) {
            for (const s of r.snapshots) {
                if (!out.some(o => o.id === s.snapshot_id)) {
                    out.push({ id: s.snapshot_id, label: `The index of the ${when(r.created_at)} run (kept for a week once replaced)` });
                }
            }
        }
        return out;
    }, [kb.state, previous]);
    const [snaps, setSnaps] = useState<string[]>(() => (kb.state.active_snapshot ? [kb.state.active_snapshot] : []));
    const [rerank, setRerank] = useState<'served' | 'off'>('served');
    const [grade, setGrade] = useState(false);
    const [answerModel, setAnswerModel] = useState('');
    const [judgeModel, setJudgeModel] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const options: ComboboxOption[] = useMemo(() => models?.chatModels ?? [], [models]);
    const answerer = answerModel || models?.model || '';
    const body: EvalRunRequest = {
        set_id: set.id, snapshot_ids: snaps, rerank: rerank === 'off' ? false : null,
        judge: grade && answerer && judgeModel ? { answer_model: answerer, judge_model: judgeModel } : null,
    };
    const priced = useEstimate(kb.id, snaps.length ? { run: body } : null);

    const start = async () => {
        setBusy(true);
        setError('');
        try {
            onStarted(await knowledgeApi.eval.startRun(kb.id, body));
            onClose();
        } catch (e) {
            setError(message(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal open onClose={onClose} title={`Run ${set.name}`} size="lg"
            description={`Searches each of its ${count(set.cases.length, 'question')} once in each index you pick, and scores where the answer ranked.`}
            footer={<>
                <Button variant="ghost" onClick={onClose}>Cancel</Button>
                <Button onClick={start} disabled={busy || !snaps.length || (grade && !body.judge)}>
                    {busy ? <Spinner /> : <Play className="size-4" aria-hidden />}Run
                </Button>
            </>}>
            <div className="space-y-4">
                <fieldset className="space-y-2">
                    <legend className="mb-1 text-sm font-medium text-text">Indexes to search</legend>
                    {choices.length === 0 && <Muted>Nothing is indexed yet.</Muted>}
                    {choices.map(c => (
                        <div key={c.id} className="flex items-center gap-2">
                            <Checkbox id={`snap-${c.id}`} checked={snaps.includes(c.id)} label={c.label}
                                onChange={on => setSnaps(prev => (on ? [...prev, c.id].slice(0, 3) : prev.filter(x => x !== c.id)))} />
                            <label htmlFor={`snap-${c.id}`} className="text-sm text-text">{c.label}</label>
                            <span className="font-mono text-xs text-text-muted" translate="no">{shortSnap(c.id)}</span>
                        </div>
                    ))}
                </fieldset>
                <Field label="Rerank">
                    <Select value={rerank} onChange={setRerank} aria-label="Rerank" options={[
                        { value: 'served', label: 'As this knowledge base serves searches' },
                        { value: 'off', label: 'Off' },
                    ]} />
                </Field>
                <div className="space-y-3 rounded-md border border-border p-3">
                    <div className="flex items-center gap-2">
                        <Checkbox id="grade-answers" checked={grade} onChange={setGrade} label="Grade answers" />
                        <label htmlFor="grade-answers" className="text-sm text-text">Grade answers (paid)</label>
                    </div>
                    <Muted className="block text-xs">
                        An answer model answers each question from the passages an agent would get; a judge from
                        another model family scores whether it says only what they say.
                    </Muted>
                    {grade && (
                        <div className="grid gap-3 sm:grid-cols-2">
                            <Field label="Answer model">
                                <Combobox value={answerer || undefined} onChange={setAnswerModel}
                                    options={withUnreachable(options, answerer || undefined)} aria-label="Answer model" />
                            </Field>
                            <Field label="Judge model" hint="From another family than the answer model.">
                                <Combobox value={judgeModel || undefined} onChange={setJudgeModel}
                                    options={withUnreachable(options, judgeModel || undefined)} aria-label="Judge model" />
                            </Field>
                        </div>
                    )}
                </div>
                <EstimateLine {...priced} />
                {error && <ErrorNote>{error}</ErrorNote>}
            </div>
        </Modal>
    );
}

// ── results ───────────────────────────────────────────────────────────────────

const COLUMNS: { key: string; label: string; get: (s: EvalScores) => number | undefined; fmt: (n?: number) => string }[] = [
    { key: 'h1', label: 'Hit@1', get: s => s.hit_at['1'], fmt: pct },
    { key: 'h5', label: 'Hit@5', get: s => s.hit_at['5'], fmt: pct },
    { key: 'h10', label: 'Hit@10', get: s => s.hit_at['10'], fmt: pct },
    { key: 'mrr', label: 'MRR', get: s => s.mrr, fmt: dec },
    { key: 'ndcg', label: 'nDCG@10', get: s => s.ndcg_at_10, fmt: dec },
    { key: 'rec', label: 'Recall@10', get: s => s.recall_at_10, fmt: pct },
];

function Results({ kb, runs, run, runId, onPick, onCancel, notify }: {
    kb: KnowledgeBase; runs: EvalRunSummary[]; run: EvalRun | null; runId: string | null;
    onPick: (id: string) => void; onCancel?: () => void; notify: Notify;
}) {
    const [kind, setKind] = useState<'all' | EvalCaseKind>('all');
    const [custom, setCustom] = useState<EvalConfigResult[]>([]);
    const [lexical, setLexical] = useState('1');
    const [vector, setVector] = useState('1');
    const [rrfK, setRrfK] = useState('60');
    const [trying, setTrying] = useState(false);
    useEffect(() => { setCustom([]); }, [runId]);

    const rows = useMemo(() => [...(run?.results ?? []), ...custom], [run, custom]);
    const scoresOf = (r: EvalConfigResult) => (kind === 'all' ? r.scores : r.by_kind[kind]);
    const best = useMemo(() => {
        const out: Record<string, number> = {};
        for (const c of COLUMNS) {
            const values = rows.filter(r => r.available && scoresOf(r)).map(r => c.get(scoresOf(r)!) ?? -1);
            out[c.key] = values.length ? Math.max(...values) : -1;
        }
        return out;
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [rows, kind]);
    const several = (run?.snapshots.length ?? 0) > 1;
    const previous = run && runs.find(r => r.status === 'completed' && r.id !== run.id
        && (r.created_at ?? '') < (run.created_at ?? ''));
    const delta = (() => {
        if (!run || !previous) return null;
        const now = run.results.find(r => r.config === 'served');
        const then = previous.results.find(r => r.config === 'served');
        if (!now?.available || !then?.available) return null;
        return now.scores.hit_at['5'] - then.scores.hit_at['5'];
    })();

    const tryWeights = async () => {
        if (!run) return;
        setTrying(true);
        try {
            const out = await knowledgeApi.eval.compare(kb.id, run.id, {
                weights: { lexical: Number(lexical) || 0, vector: Number(vector) || 0 }, rrf_k: Number(rrfK) || 60,
            });
            setCustom(prev => [...prev.filter(p => !out.some(o => o.key === p.key)), ...out]);
        } catch (e) {
            notify(message(e), 'danger');
        } finally {
            setTrying(false);
        }
    };

    return (
        <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
                <Label>Results</Label>
                <Select size="sm" className="w-auto max-w-sm" value={runId ?? ''} onChange={onPick} aria-label="Run"
                    options={runs.map(r => ({ value: r.id, label: `${when(r.created_at)} · ${RUN_STATUS[r.status] ?? r.status}${r.started_by === 'settings' ? ' · after a settings change' : ''}` }))} />
                {run && run.results.length > 0 && (
                    <Select size="sm" className="w-auto" value={kind} onChange={setKind} options={KIND_FILTER} aria-label="Questions to score" />
                )}
            </div>
            {!run ? <div className="flex justify-center py-4"><Spinner /></div> : (
                <>
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm" aria-live="polite">
                        <Badge size="sm" tone={run.status === 'completed' ? 'success' : run.status === 'failed' ? 'danger' : 'neutral'}>
                            {RUN_STATUS[run.status] ?? run.status}
                        </Badge>
                        {ACTIVE.has(run.status) ? (
                            <>
                                <Muted>{run.done_count} of {count(run.case_count, 'question')}</Muted>
                                {run.status !== 'cancelling' && onCancel && (
                                    <Button size="sm" variant="ghost" onClick={onCancel}><Square className="size-3.5" aria-hidden />Stop</Button>
                                )}
                            </>
                        ) : (
                            <Muted>
                                {count(run.compared_cases, 'question')} scored
                                {run.stale_cases ? ` · ${run.stale_cases} no longer in their document` : ''}
                                {run.cost_usd != null ? ` · cost ${formatUsd(run.cost_usd)}` : ''}
                                {delta != null && Math.abs(delta) >= 0.005 ? ` · hit@5 ${delta > 0 ? '+' : ''}${Math.round(delta * 100)} points since the run before` : ''}
                            </Muted>
                        )}
                    </div>
                    {run.error && <ErrorNote>{run.error}</ErrorNote>}
                    {run.notes.length > 0 && (
                        <ul className="space-y-0.5 text-xs text-text-muted">{run.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
                    )}
                    {rows.length > 0 && (
                        <Table density="compact" minWidth="36rem">
                            <THead>
                                <TR hover={false}>
                                    <TH width="full">Search</TH>
                                    {several && <TH width="min">Index</TH>}
                                    {COLUMNS.map(c => <TH key={c.key} width="min" align="right">{c.label}</TH>)}
                                </TR>
                            </THead>
                            <TBody>
                                {rows.map(r => {
                                    const s = scoresOf(r);
                                    return (
                                        <TR key={r.key}>
                                            <TD><span className="text-sm text-text">{r.label}</span></TD>
                                            {several && (() => {
                                                const snap = run.snapshots.find(x => x.snapshot_id === r.snapshot_id);
                                                return (
                                                    <TD nowrap>
                                                        <span className="text-xs text-text" title={r.snapshot_id}>{SNAP_STATUS[snap?.status ?? ''] ?? snap?.status}</span>
                                                        {snap?.chunker_version && <span className="block font-mono text-xs text-text-muted" translate="no">{snap.chunker_version}</span>}
                                                    </TD>
                                                );
                                            })()}
                                            {!r.available || !s ? (
                                                <TD align="right" nowrap><Muted className="text-xs">{r.reason ?? 'No questions of this kind'}</Muted></TD>
                                            ) : COLUMNS.map(c => {
                                                const v = c.get(s);
                                                return (
                                                    <TD key={c.key} align="right" nowrap>
                                                        <span className={cn('tabular-nums', v != null && v === best[c.key] && v > 0 && 'font-semibold text-text')}>{c.fmt(v)}</span>
                                                    </TD>
                                                );
                                            })}
                                        </TR>
                                    );
                                })}
                            </TBody>
                        </Table>
                    )}
                    {(() => {
                        const sees = rows.find(r => r.config === 'served')?.agent_sees;
                        return sees != null && sees < 10 ? (
                            <Muted className="block text-xs">
                                As served, an agent gets the top {sees}: hits further down are this test&apos;s own view.
                            </Muted>
                        ) : null;
                    })()}
                    {run.judge && <JudgeLine judge={run.judge} />}
                    {run.status === 'completed' || run.status === 'cancelled' ? (
                        <div className="flex flex-wrap items-end gap-2">
                            <Field label="Keyword weight"><Input className="w-24" type="number" min={0} step={0.1} value={lexical} onChange={e => setLexical(e.target.value)} /></Field>
                            <Field label="Meaning weight"><Input className="w-24" type="number" min={0} step={0.1} value={vector} onChange={e => setVector(e.target.value)} /></Field>
                            <Field label="k"><Input className="w-20" type="number" min={1} max={1000} value={rrfK} onChange={e => setRrfK(e.target.value)} /></Field>
                            <Button size="sm" variant="secondary" onClick={tryWeights} disabled={trying}>
                                <ClipboardCheck className="size-4" aria-hidden />Try weights ($0)
                            </Button>
                        </div>
                    ) : null}
                    {run.cases.length > 0 && <CaseList kb={kb} run={run} />}
                </>
            )}
        </div>
    );
}

function JudgeLine({ judge }: { judge: NonNullable<EvalRun['judge']> }) {
    return (
        <div className="space-y-1 rounded-md border border-border p-3 text-sm">
            <div>
                <span className="text-text">Grounded in cited sources: </span>
                <span className="tabular-nums">{judge.mean_score == null ? '—' : `${judge.mean_score.toFixed(1)} / ${judge.scale_max}`}</span>
                <Muted> · {count(judge.judged, 'answer')} judged{judge.failed ? `, ${judge.failed} not` : ''}</Muted>
            </div>
            <Muted className="block text-xs">
                Quotes found word for word: {judge.quotes_matched} of {judge.quotes}
                {judge.unresolved_labels ? ` · ${judge.unresolved_labels} cited labels no passage has` : ''} · answers by {judge.answer_model}, judged by {judge.judge_model}
            </Muted>
            {judge.warning && <Muted className="block text-xs">{judge.warning}</Muted>}
        </div>
    );
}

function CaseList({ kb, run }: { kb: KnowledgeBase; run: EvalRun }) {
    const first = run.snapshots[0]?.snapshot_id ?? '';
    const order: [string, string][] = [['served', 'As served'], ['hybrid', 'Hybrid'], ['lexical', 'Keyword'], ['vector', 'Meaning']];
    return (
        <details className="rounded-md border border-border px-3 py-2">
            <summary className="cursor-pointer select-none text-sm text-text-muted">Each question ({run.cases.length})</summary>
            <ul className="mt-2 divide-y divide-border">
                {run.cases.map(c => (
                    <li key={c.case_id} className="space-y-1 py-2 text-sm">
                        <div className="flex flex-wrap items-baseline gap-2">
                            <span className="text-text">{c.question}</span>
                            <Badge size="sm">{EVAL_KINDS[c.kind]}</Badge>
                            {c.stale && <Badge size="sm" tone="warning">No longer in its document</Badge>}
                            {c.trimmed && <Badge size="sm">Trace trimmed</Badge>}
                        </div>
                        <Muted className="block text-xs">
                            {order.map(([k, name]) => {
                                const rank = c.ranks[`${first}:${k}`];
                                return `${name} ${rank == null ? (k in c.ranks ? 'not found' : '—') : `#${rank}`}`;
                            }).join(' · ')}
                        </Muted>
                        {c.top.length > 0 && (
                            <ol className="space-y-0.5 text-xs">
                                {c.top.map((t, i) => (
                                    <li key={i} className={cn('truncate', t.relevant ? 'text-text' : 'text-text-muted')}>
                                        {i + 1}. <span className="font-mono" translate="no">{inBase(t.path, kb.folder)}</span>{t.relevant ? ' — holds the answer' : ''}
                                    </li>
                                ))}
                            </ol>
                        )}
                        {c.answer && (
                            <div className="rounded bg-surface-2 p-2 text-xs">
                                <div className="whitespace-pre-line text-text">{c.answer}</div>
                                {c.verdict && (
                                    <Muted className="mt-1 block">
                                        {c.verdict.error ?? `Judge: ${c.verdict.score?.toFixed(1)} — ${c.verdict.reasoning}`}
                                    </Muted>
                                )}
                            </div>
                        )}
                        {c.query_ids[first] && <CaseTrace queryId={c.query_ids[first]} />}
                    </li>
                ))}
            </ul>
        </details>
    );
}

function CaseTrace({ queryId }: { queryId: string }) {
    const [trace, setTrace] = useState<SearchTrace | null | 'gone'>(null);
    const [open, setOpen] = useState(false);
    useEffect(() => {
        if (!open || trace) return;
        getQueryTrace(queryId).then(setTrace).catch(() => setTrace('gone'));
    }, [open, queryId, trace]);
    return (
        <details className="text-xs" onToggle={e => setOpen((e.target as HTMLDetailsElement).open)}>
            <summary className="cursor-pointer select-none text-text-muted">Trace · <span className="font-mono" translate="no">{queryId}</span></summary>
            {trace === 'gone' ? <Muted className="mt-1 block">This search&apos;s trace has expired (searches are kept 30 days).</Muted>
                : trace ? <RetrievalTrace trace={trace} className="mt-2" /> : open ? <Spinner /> : null}
        </details>
    );
}
