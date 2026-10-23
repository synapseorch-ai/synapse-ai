'use client';

/*
 * Knowledge: a Vault folder made searchable (D157).
 *
 * The left column lists the knowledge bases; the right shows the selected one's
 * documents — every file in its folder, with where it stands — and takes
 * uploads. A document opens a drawer with its ingestion report: what happened
 * to it, stage by stage, and whether retrying can help.
 *
 * Web pages (S1, D165) come in through "Add web pages": a page, a sitemap's
 * pages, or a site crawled from a URL. Each URL is a web source, listed with
 * its latest crawl; every fetched page is a document like any other, with its
 * URL beside it.
 *
 * Polls every 2 s while anything is still being worked on, else every 10 s, as
 * the Repos screen does.
 *
 * A base's settings open in a drawer (`KnowledgeSettings`, B2b). A change to how
 * its index is built rebuilds it beside the serving one; the line under the
 * header follows that rebuild, and the Costs section says what the base has spent.
 *
 * Progress (PG1, F357): an upload shows its bytes and the time left from the rate
 * it is sending at; a working document its stage, a bar and the time left; a
 * rebuild its passages; and each says when it waits out a provider's rate limit.
 * No estimate is shown before there is a pace to estimate from.
 *
 * Evaluate (`KnowledgeEval`, V1) tests the base on its own documents: question sets, runs scored with no judge,
 * snapshots side by side, and an optional grounded judge.
 *
 * This directory is shared with the cloud app byte for byte; what differs between the products (the workspace's
 * models, web sources and Evaluate being served, expiring part URLs) comes from `./host` (D175).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    Database, ExternalLink, FileText, FolderOpen, Globe, Library, Plus, RotateCcw, Search, SlidersHorizontal, Trash2, Upload,
} from 'lucide-react';

import {
    Badge, Button, ConfirmDialog, Drawer, EmptyState, ErrorNote, Field, IconButton, Input, Label, Modal, Mono,
    Muted, Screen, Section, Select, Spinner, StatStrip, TBody, TD, TH, THead, TR, Table, TableActions, Textarea, Toast, useToast,
    type SelectOption,
} from '@/components/ui';
import { useHelpTopic } from '@/components/help';
import { RetrievalTrace } from '@/components/RetrievalTrace';
import { when as runWhen } from '@/components/run/model';
import { count } from '@/lib/copy';
import { cn } from '@/lib/cn';
import {
    ACCEPTED, BUILD_PAUSED, CRAWLING, DEGRADED, PRESETS, PROGRESS_STAGE, REASONS, RESTING, STATUS, WORKING, formatBytes,
    formatUsd, inBase, knowledgeApi, progressCount, timeLeft, uploadToKnowledge, useKnowledgeEnabled, vaultFolders,
    waitNote,
    type CostGroup, type CostGroupBy, type DocStatus, type DocumentProgress, type KnowledgeBase, type KnowledgeDocument,
    type KnowledgeDocumentDetail, type SearchResult, type WebCrawlState, type WebSourceKind,
} from '@/lib/knowledge';

import { useKnowledgeHost } from './host';
import { ProgressBar } from './Progress';
import { Evaluate } from './KnowledgeEval';
import { KnowledgeSettings } from './KnowledgeSettings';

/** "Today 7:25 PM", "Mon 5 Oct, 7:25 PM" (UX1's dates, as the run screens print them). Rendered in the browser only. */
function when(iso: string | null | undefined): string {
    return iso ? runWhen(iso, { now: Date.now() }) : '—';
}

/** The documents as rows, the runs list's way (M2e, Naveen): a status dot, the file and its line, then the numbers. */
const DOC_GRID = 'grid items-center gap-x-3.5 px-2.5';
const DOC_COLS = 'grid-cols-[14px_minmax(0,1fr)] sm:grid-cols-[14px_minmax(0,1fr)_4.5rem_9.5rem]';
const DOC_COLS_EDIT = 'grid-cols-[14px_minmax(0,1fr)_auto] sm:grid-cols-[14px_minmax(0,1fr)_4.5rem_9.5rem_4.5rem]';
/** A dot for where a document stands: green indexed, cyan being worked on, red failed, grey resting otherwise. */
const DOC_DOT: Partial<Record<DocStatus, string>> = {
    indexed: 'bg-success', partial: 'bg-text-muted', queued: 'bg-machine', parsing: 'bg-machine', indexing: 'bg-machine',
    failed: 'bg-danger', blocked: 'bg-danger', skipped: 'bg-text-faint', paused: 'bg-text-faint', deleting: 'bg-text-faint',
};

/** Where a document came from, as the Documents pills group it (M2e): a file, a fetched page, or a record. */
type SourceKind = 'file' | 'web' | 'record';
const sourceKind = (d: KnowledgeDocument): SourceKind => (d.source === 'web' ? 'web' : d.source === 'record' ? 'record' : 'file');
const SOURCES: { value: SourceKind; label: string; icon: typeof FileText }[] = [
    { value: 'file', label: 'Files', icon: FileText },
    { value: 'web', label: 'Web pages', icon: Globe },
    { value: 'record', label: 'Records', icon: Database },
];

/** A URL as a person reads it: host and path, no scheme. */
function shortUrl(url: string): string {
    try {
        const u = new URL(url);
        return `${u.host}${u.pathname === '/' ? '' : u.pathname}${u.search}`;
    } catch {
        return url;
    }
}

function WebLink({ url, className }: { url: string; className?: string }) {
    return (
        <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className={cn('inline-flex min-w-0 max-w-full items-center gap-1 text-xs text-text-muted hover:text-text hover:underline', className)}
        >
            <span className="truncate" translate="no">{shortUrl(url)}</span>
            <ExternalLink className="size-3 shrink-0" aria-hidden />
        </a>
    );
}

function StatusBadge({ status }: { status: DocStatus }) {
    const s = STATUS[status] ?? { label: status, tone: 'neutral' as const };
    return <Badge tone={s.tone} dot size="sm">{s.label}</Badge>;
}

function tally(kb: KnowledgeBase) {
    const c = kb.state.counts_by_status;
    return {
        total: Object.values(c).reduce((a, b) => a + (b ?? 0), 0),
        working: [...WORKING].reduce((a, s) => a + (c[s] ?? 0), 0),
        failed: (c.failed ?? 0) + (c.blocked ?? 0),
    };
}

function summary(kb: KnowledgeBase): string {
    const { total, working, failed } = tally(kb);
    if (!total) return 'No documents yet';
    const parts = [count(total, 'document')];
    if (working) parts.push(`${working} in progress`);
    if (failed) parts.push(`${failed} failed`);
    if (kb.state.month_spend_usd > 0) parts.push(`${formatUsd(kb.state.month_spend_usd)} this month`);
    return parts.join(' · ');
}

/** `tabs`: the hub's tab bar (Knowledge · Boards), as the product's frame draws it; this screen draws its own frame. */
export function KnowledgeScreen({ tabs }: { tabs?: React.ReactNode } = {}) {
    const enabled = useKnowledgeEnabled();
    const { canEdit, offHint } = useKnowledgeHost();
    const [bases, setBases] = useState<KnowledgeBase[] | null>(null);
    const [selected, setSelected] = useState<string | null>(null);
    const [creating, setCreating] = useState(false);
    const [deleting, setDeleting] = useState<KnowledgeBase | null>(null);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const { toast, show, dismiss } = useToast();

    const load = useCallback(async () => {
        try {
            const list = await knowledgeApi.bases();
            setBases(list);
            setSelected(cur => (cur && list.some(b => b.id === cur) ? cur : list[0]?.id ?? null));
            setError('');
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
            setBases(b => b ?? []);
        }
    }, []);

    useEffect(() => { if (enabled) load(); }, [enabled, load]);

    const kb = bases?.find(b => b.id === selected) ?? null;
    // The "?" panel: starting out, or working in a base (H1).
    useHelpTopic(kb ? 'knowledge.base' : null);

    const remove = async () => {
        if (!deleting) return;
        setBusy(true);
        try {
            await knowledgeApi.deleteBase(deleting.id);
            show(`${deleting.name} deleted. Its files are still in the Vault.`);
            setDeleting(null);
            await load();
        } catch (e) {
            show(e instanceof Error ? e.message : String(e), 'danger');
        } finally {
            setBusy(false);
        }
    };

    if (enabled === null) {
        return <Screen eyebrow="Build" title="Knowledge" width="wide" tabs={tabs}><div className="flex justify-center py-12"><Spinner /></div></Screen>;
    }
    if (!enabled) {
        return (
            <Screen eyebrow="Build" title="Knowledge" description="Let agents answer from your own documents, and show where each answer came from." width="wide" tabs={tabs}>
                <EmptyState title="Knowledge is off in this workspace">
                    {offHint ?? 'Knowledge is not switched on here yet.'}
                </EmptyState>
            </Screen>
        );
    }

    return (
        <Screen
            eyebrow="Build"
            title="Knowledge"
            description="Let agents answer from your own documents, and show where each answer came from."
            width="wide"
            tabs={tabs}
            actions={canEdit && <Button onClick={() => setCreating(true)}><Plus className="size-4" aria-hidden />New knowledge base</Button>}
        >
            {error && <ErrorNote className="mb-4">{error}</ErrorNote>}
            {bases === null ? (
                <div className="flex justify-center py-12"><Spinner /></div>
            ) : bases.length === 0 ? (
                <>
                <EmptyState
                    title="No knowledge bases yet"
                    action={canEdit && <Button onClick={() => setCreating(true)}>New knowledge base</Button>}
                >
                    A knowledge base is a folder of documents your agents can look things up in. Pick a Vault folder or
                    start a new one, and everything in it becomes searchable.
                </EmptyState>
                    <ol className="mx-auto -mt-4 mb-8 grid px-5 max-w-2xl gap-4 text-left sm:grid-cols-3" aria-label="How a knowledge base works">
                        {[
                            [FolderOpen, 'A folder', 'Name a Vault folder, or upload files and add web pages to it.'],
                            [Library, 'Read and made searchable', 'Every file is read and split into short passages an agent can find.'],
                            [Search, 'Searched, with sources', 'Agents that use it answer from it and cite what they used.'],
                        ].map(([Icon, head, body]) => {
                            const I = Icon as typeof Search;
                            return (
                                <li key={head as string} className="flex gap-3">
                                    <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-surface-2 text-text-muted" aria-hidden>
                                        <I className="size-4" />
                                    </span>
                                    <span className="min-w-0">
                                        <span className="block text-sm font-medium text-text">{head as string}</span>
                                        <span className="block text-xs leading-relaxed text-text-muted">{body as string}</span>
                                    </span>
                                </li>
                            );
                        })}
                    </ol>
                </>
            ) : (
                <div className="grid gap-6 lg:grid-cols-[17rem_minmax(0,1fr)] lg:gap-8">
                    <nav aria-label="Knowledge bases" className="space-y-0.5">
                        {bases.map(b => (
                            <button
                                key={b.id}
                                type="button"
                                onClick={() => setSelected(b.id)}
                                aria-current={b.id === selected ? 'true' : undefined}
                                className={cn(
                                    'w-full rounded-lg px-3 py-2.5 text-left transition-colors',
                                    b.id === selected
                                        ? 'bg-surface-2 shadow-[inset_0_0_0_1px_var(--border)]'
                                        : 'hover:bg-surface-2',
                                )}
                            >
                                <span className="flex min-w-0 items-center gap-2 text-sm font-semibold text-text">
                                    <Library className="size-4 shrink-0 text-text-faint" aria-hidden />
                                    <span className="truncate">{b.name}</span>
                                </span>
                                <span className="mt-0.5 block truncate font-mono text-xs text-text-muted">{b.folder}</span>
                                <span className="mt-0.5 block truncate text-xs text-text-faint">
                                    {b.status === 'disabled' ? 'Off · ' : ''}{summary(b)}
                                </span>
                            </button>
                        ))}
                    </nav>
                    {kb && <BaseDetail key={kb.id} kb={kb} onChanged={load} onDelete={() => setDeleting(kb)} notify={show} />}
                </div>
            )}

            <NewBaseDialog
                open={creating}
                onClose={() => setCreating(false)}
                onCreated={async created => {
                    setCreating(false);
                    show(`${created.name} created. Files already in ${created.folder} are being indexed.`);
                    await load();
                    setSelected(created.id);
                }}
            />
            <ConfirmDialog
                open={!!deleting}
                onClose={() => setDeleting(null)}
                onConfirm={remove}
                busy={busy}
                title={`Delete ${deleting?.name ?? 'knowledge base'}?`}
                description={deleting ? `Its documents leave the index. The files in ${deleting.folder} stay in the Vault.` : undefined}
            />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </Screen>
    );
}

function NewBaseDialog({
    open, onClose, onCreated,
}: {
    open: boolean;
    onClose: () => void;
    onCreated: (kb: KnowledgeBase) => void;
}) {
    const [name, setName] = useState('');
    const [folder, setFolder] = useState('');
    const [description, setDescription] = useState('');
    const [preset, setPreset] = useState('balanced');
    const [known, setKnown] = useState<string[]>([]);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        if (!open) return;
        setName(''); setFolder(''); setDescription(''); setPreset('balanced'); setError('');
        fetch('/api/vault/tree')
            .then(r => r.json())
            .then(data => setKnown(vaultFolders(data).filter(p => !/^(tool_outputs|playwright|compaction_archives)(\/|$)/.test(p))))
            .catch(() => setKnown([]));
    }, [open]);

    const create = async () => {
        setBusy(true);
        setError('');
        try {
            const fields = PRESETS.find(p => p.id === preset)?.fields ?? {};
            onCreated(await knowledgeApi.createBase({ name: name.trim(), folder: folder.trim(), description, ...fields }));
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <Modal
            open={open}
            onClose={onClose}
            title="New knowledge base"
            description="A Vault folder, and everything in it, made searchable."
            footer={
                <>
                    <Button variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
                    <Button onClick={create} loading={busy} disabled={!name.trim() || !folder.trim()}>Create</Button>
                </>
            }
        >
            <div className="space-y-4">
                {error && <ErrorNote>{error}</ErrorNote>}
                <Field label="Name" required>
                    <Input value={name} onChange={e => setName(e.target.value)} placeholder="Contracts" maxLength={200} />
                </Field>
                <Field
                    label="Vault folder"
                    required
                    hint="An existing folder, or a new one to create. It cannot overlap another knowledge base."
                >
                    <Input
                        value={folder}
                        onChange={e => setFolder(e.target.value)}
                        placeholder="contracts"
                        list="knowledge-folders"
                    />
                </Field>
                <datalist id="knowledge-folders">
                    {known.map(p => <option key={p} value={p} />)}
                </datalist>
                <Field label="Description" hint="Optional. What is in it, for the people and agents who use it.">
                    <Textarea value={description} onChange={e => setDescription(e.target.value)} rows={2} maxLength={2000} />
                </Field>
                <Field label="Preset" hint={PRESETS.find(p => p.id === preset)?.description}>
                    <Select
                        value={preset}
                        onChange={setPreset}
                        options={PRESETS.map(p => ({ value: p.id, label: p.name }))}
                        aria-label="Preset"
                    />
                </Field>
            </div>
        </Modal>
    );
}

/** An upload under way: `samples` are (when, bytes sent) over the last few seconds, for its rate. */
type Uploading = { id: number; name: string; sent: number; total: number; started: number; samples: [number, number][];
    error?: string };

/** How long an upload's rate is measured over, how long before it is trusted, and how often a row is redrawn. */
const RATE_WINDOW_MS = 5000;
const RATE_AFTER_MS = 2000;
const REDRAW_MS = 250;

/** "3.1 MB of 12 MB · about 2 min left", or "Finishing…" once every byte is sent and the upload is completed. */
function uploadLine(u: Uploading): string {
    if (u.sent >= u.total) return 'Finishing…';
    const sent = `${formatBytes(u.sent)} of ${formatBytes(u.total)}`;
    const first = u.samples[0], last = u.samples[u.samples.length - 1];
    if (!first || !last || last[0] - u.started < RATE_AFTER_MS || last[0] <= first[0]) return sent;
    const rate = (last[1] - first[1]) / ((last[0] - first[0]) / 1000);
    const left = rate > 0 ? timeLeft((u.total - u.sent) / rate) : null;
    return left ? `${sent} · ${left}` : sent;
}

/** A working document's stage, its bar, how far it has got, the time left, and a rate limit's wait. */
function DocProgress({ progress, name }: { progress: DocumentProgress; name: string }) {
    const stage = PROGRESS_STAGE[progress.stage] ?? progress.stage;
    const wait = waitNote(progress.waiting);
    const line = [stage, progressCount(progress), wait ? null : timeLeft(progress.eta_s)].filter(Boolean).join(' · ');
    return (
        <div className="mt-1 space-y-1">
            <ProgressBar value={progress.done} max={progress.total} label={`${stage}: ${name}`} className="max-w-xs" />
            <span className="block truncate text-xs text-text-muted">{line}</span>
            {wait && <span className="block text-xs text-warning">{wait}</span>}
        </div>
    );
}

function BaseDetail({
    kb, onChanged, onDelete, notify,
}: {
    kb: KnowledgeBase;
    onChanged: () => void;
    onDelete: () => void;
    notify: (message: string, tone?: 'success' | 'warning' | 'danger') => void;
}) {
    const host = useKnowledgeHost();
    const [docs, setDocs] = useState<KnowledgeDocument[] | null>(null);
    const [crawls, setCrawls] = useState<WebCrawlState[]>([]);
    const [addingWeb, setAddingWeb] = useState(false);
    const [tick, setTick] = useState(0);
    const [filter, setFilter] = useState<'all' | DocStatus>('all');
    const [kind, setKind] = useState<'all' | SourceKind>('all');
    // The served index's top-5 rate from Evaluate's selected question set; null until Evaluate has read one.
    const [hit5, setHit5] = useState<number | null>(null);
    const [open, setOpen] = useState<string | null>(null);
    const [fetched, setFetched] = useState<KnowledgeDocumentDetail | null>(null);
    const detail = fetched && fetched.id === open ? fetched : null;
    const [removing, setRemoving] = useState<KnowledgeDocument | null>(null);
    const [uploads, setUploads] = useState<Uploading[]>([]);
    const uploadIds = useRef(0);
    const [dragging, setDragging] = useState(false);
    const [editing, setEditing] = useState(false);
    const input = useRef<HTMLInputElement>(null);

    const refresh = useCallback(() => setTick(t => t + 1), []);

    useEffect(() => {
        let live = true;
        knowledgeApi.documents(kb.id)
            .then(list => { if (live) setDocs(list); })
            .catch(e => notify(e instanceof Error ? e.message : String(e), 'danger'));
        if (host.webSources) {
            knowledgeApi.crawls(kb.id)
                .then(list => { if (live) setCrawls(list); })
                .catch(() => { if (live) setCrawls([]); });
        }
        return () => { live = false; };
    }, [kb.id, tick, notify, host.webSources]);

    const build = kb.state.build ?? null;
    const rebuilding = build?.status === 'building' && !build.paused_reason;
    const working = !!docs?.some(d => WORKING.has(d.status)) || uploads.some(u => !u.error)
        || crawls.some(c => CRAWLING.has(c.status)) || rebuilding;
    useEffect(() => {
        const timer = setInterval(() => { refresh(); onChanged(); }, working ? 2000 : 10000);
        return () => clearInterval(timer);
    }, [working, refresh, onChanged]);

    useEffect(() => {
        if (!open) return;
        let live = true;
        knowledgeApi.document(open).then(d => { if (live) setFetched(d); }).catch(() => { });
        return () => { live = false; };
    }, [open, docs]);

    const send = async (files: FileList | File[]) => {
        const list = [...files].map(file => ({ file, id: ++uploadIds.current }));
        if (!list.length) return;
        const now = Date.now();
        setUploads(u => [...u, ...list.map(({ file, id }) => ({
            id, name: file.name, sent: 0, total: file.size, started: now, samples: [] as [number, number][],
        }))]);
        for (const { file, id } of list) {
            let drawn = 0;
            const heard = (sent: number, total: number) => {
                const at = Date.now();
                if (at - drawn < REDRAW_MS && sent < total) return;  // a part's bytes arrive many times a second
                drawn = at;
                setUploads(u => u.map(x => x.id !== id ? x : {
                    ...x, sent, total,
                    started: x.samples.length ? x.started : at,  // the clock starts with the first byte, not the queue
                    samples: [...x.samples.filter(([t]) => at - t <= RATE_WINDOW_MS), [at, sent] as [number, number]],
                }));
            };
            try {
                await uploadToKnowledge(kb.id, file, { onProgress: heard, refreshParts: host.refreshParts });
                setUploads(u => u.filter(x => x.id !== id));
            } catch (e) {
                const message = e instanceof Error ? e.message : String(e);
                setUploads(u => u.map(x => (x.id === id ? { ...x, error: message } : x)));
            }
        }
        refresh();
        onChanged();
    };

    const retry = async (doc: KnowledgeDocument) => {
        try {
            await knowledgeApi.retry(doc.id);
            refresh();
        } catch (e) {
            notify(e instanceof Error ? e.message : String(e), 'danger');
        }
    };

    const retryRebuild = async () => {
        try {  // an edit that changes nothing re-checks the index, and starts the rebuild again
            await knowledgeApi.updateBase(kb.id, { revision: kb.revision });
            onChanged();
        } catch (e) {
            notify(e instanceof Error ? e.message : String(e), 'danger');
        }
    };

    const remove = async () => {
        if (!removing) return;
        try {
            await knowledgeApi.deleteDocument(removing.id);
            notify(`${inBase(removing.path, kb.folder)} deleted.`);
            if (open === removing.id) setOpen(null);
            setRemoving(null);
            refresh();
            onChanged();
        } catch (e) {
            notify(e instanceof Error ? e.message : String(e), 'danger');
        }
    };

    const statuses = useMemo(() => {
        const seen = new Set(docs?.map(d => d.status));
        const options: SelectOption<'all' | DocStatus>[] = [{ value: 'all', label: 'All statuses' }];
        for (const s of Object.keys(STATUS) as DocStatus[]) {
            if (seen.has(s)) options.push({ value: s, label: STATUS[s].label });
        }
        return options;
    }, [docs]);
    const kinds = useMemo(() => {
        const n: Record<SourceKind, number> = { file: 0, web: 0, record: 0 };
        for (const d of docs ?? []) n[sourceKind(d)] += 1;
        return n;
    }, [docs]);
    const present = SOURCES.filter(k => kinds[k.value] > 0);
    const shown = (docs ?? [])
        .filter(d => filter === 'all' || d.status === filter)
        .filter(d => kind === 'all' || sourceKind(d) === kind)
        .sort((a, b) => a.path.localeCompare(b.path));
    const { total, working: inProgress, failed } = tally(kb);
    const spent = kb.state.month_spend_usd;
    const cap = kb.budget_monthly_usd;

    return (
        <section aria-label={kb.name} className="min-w-0 space-y-5">
            <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
                <div className="flex min-w-0 items-start gap-3.5">
                    <span className="grid size-11 shrink-0 place-items-center rounded-xl bg-machine-subtle text-machine" aria-hidden>
                        <Library className="size-5" />
                    </span>
                    <div className="min-w-0">
                        <h2 className="truncate text-xl font-semibold tracking-[-0.02em] text-text">{kb.name}</h2>
                        <p className="mt-0.5 flex min-w-0 items-center gap-1.5 text-sm text-text-muted">
                            <FolderOpen className="size-3.5 shrink-0" aria-hidden />
                            <Mono className="truncate">{kb.folder}</Mono>
                            {kb.status === 'disabled' && <Muted>· Off</Muted>}
                        </p>
                        {kb.description && <p className="mt-1 max-w-[70ch] text-sm text-text-muted">{kb.description}</p>}
                    </div>
                </div>
                {host.canEdit && (
                    <div className="flex flex-wrap gap-2">
                        <Button variant="secondary" onClick={() => input.current?.click()}>
                            <Upload className="size-4" aria-hidden />Upload files
                        </Button>
                        {host.webSources && (
                            <Button variant="secondary" onClick={() => setAddingWeb(true)} disabled={kb.status !== 'active'}>
                                <Globe className="size-4" aria-hidden />Add web pages
                            </Button>
                        )}
                        <Button variant="secondary" onClick={() => setEditing(true)}>
                            <SlidersHorizontal className="size-4" aria-hidden />Settings
                        </Button>
                        <Button variant="ghost" onClick={onDelete}>
                            <Trash2 className="size-4" aria-hidden />Delete
                        </Button>
                    </div>
                )}
            </div>

            <div><StatStrip
                size="sm"
                label={`${kb.name} at a glance`}
                cells={[
                    {
                        label: 'Documents',
                        value: total.toLocaleString(),
                        sub: [inProgress && `${inProgress} in progress`, failed && `${failed} failed`].filter(Boolean).join(' · ') || undefined,
                        dot: failed ? 'danger' : inProgress ? 'machine' : undefined,
                    },
                    {
                        label: 'This month',
                        value: formatUsd(spent),
                        sub: cap != null ? `of a ${formatUsd(cap)} budget` : 'Indexing and searches',
                        dot: cap != null && spent >= cap ? 'danger' : undefined,
                    },
                    {
                        label: 'Finds the answer',
                        value: hit5 == null ? '—' : `${Math.round(hit5 * 100)}%`,
                        sub: hit5 == null ? 'Not measured' : 'In the top 5, last evaluation',
                    },
                ]}
            /></div>

            {build && <RebuildLine build={build} onRetry={host.canEdit ? retryRebuild : undefined} />}

            {host.canEdit && (
                <>
                    <input
                        ref={input}
                        type="file"
                        multiple
                        accept={ACCEPTED.join(',')}
                        className="hidden"
                        aria-hidden
                        tabIndex={-1}
                        onChange={e => { if (e.target.files) send(e.target.files); e.target.value = ''; }}
                    />
                    <div
                        onDragOver={e => { e.preventDefault(); setDragging(true); }}
                        onDragLeave={() => setDragging(false)}
                        onDrop={e => { e.preventDefault(); setDragging(false); send(e.dataTransfer.files); }}
                        className={cn(
                            'flex items-start gap-2.5 rounded-lg border border-dashed px-3.5 py-2.5 text-sm text-text-muted transition-colors',
                            dragging ? 'border-accent bg-accent-subtle text-text' : 'border-border bg-surface-2/60',
                        )}
                    >
                        <Upload className="mt-0.5 size-4 shrink-0 text-text-faint" aria-hidden />
                        <span>
                            Drop files here to add them to <span className="font-mono">{kb.folder}</span>. Documents, spreadsheets,
                            slides, email, web pages and code are read{host.ocr
                                ? ', and scanned pages and images by OCR.'
                                : '. Scanned pages and images aren\'t read: this install can\'t read text in pictures.'}
                        </span>
                    </div>
                </>
            )}
            {uploads.length > 0 && (
                <ul className="space-y-2" aria-label="Uploads">
                    {uploads.map(u => (
                        <li key={u.id} className="space-y-1 text-sm">
                            <div className="flex items-center gap-2">
                                <span className="min-w-0 flex-1 truncate">{u.name}</span>
                                {u.error ? (
                                    <>
                                        <span className="text-danger">{u.error}</span>
                                        <IconButton size="sm" label={`Dismiss ${u.name}`} icon={Trash2}
                                            onClick={() => setUploads(x => x.filter(y => y.id !== u.id))} />
                                    </>
                                ) : (
                                    <Muted>{uploadLine(u)}</Muted>
                                )}
                            </div>
                            {!u.error && <ProgressBar value={u.sent} max={u.total} label={`Uploading ${u.name}`} />}
                        </li>
                    ))}
                </ul>
            )}

            <TestSearch kb={kb} />

            <div className="mb-0 space-y-3 border-t border-border pb-5 pt-5">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <h3 className="text-section text-text">{docs ? count(shown.length, 'document') : 'Documents'}</h3>
                {present.length > 1 && (
                    <div role="group" aria-label="Show documents" className="flex flex-wrap items-center gap-1.5">
                        {[{ value: 'all' as const, label: 'All', n: docs?.length ?? 0, icon: null }, ...present.map(k => ({ ...k, n: kinds[k.value] }))].map(k => (
                            <button
                                key={k.value}
                                type="button"
                                aria-pressed={kind === k.value}
                                onClick={() => setKind(k.value)}
                                className={cn(
                                    'inline-flex h-7 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 text-[13px] font-medium transition-colors',
                                    kind === k.value
                                        ? 'border-transparent bg-text text-bg'
                                        : 'border-border text-text-muted hover:border-border-strong hover:text-text',
                                )}
                            >
                                {k.icon && <k.icon className="size-3.5" aria-hidden />}
                                {k.label}
                                <span className="text-[11.5px] tabular-nums opacity-70">{k.n}</span>
                            </button>
                        ))}
                    </div>
                )}
                <span className="hidden flex-1 sm:block" />
                <div className="w-44">
                    <Select size="sm" value={filter} onChange={setFilter} options={statuses} aria-label="Filter by status" />
                </div>
            </div>

            {docs === null ? (
                <div className="flex justify-center py-8"><Spinner /></div>
            ) : shown.length === 0 ? (
                <EmptyState title={docs.length ? (filter !== 'all' ? 'No documents with this status' : 'No documents of this kind') : 'No documents yet'}>
                    {docs.length || !host.canEdit ? null : 'Upload files, or add them to the folder in the Vault.'}
                </EmptyState>
            ) : (
                <div role="table" aria-label="Documents" aria-rowcount={shown.length + 1}>
                    <div role="row" className={cn(DOC_GRID, host.canEdit ? DOC_COLS_EDIT : DOC_COLS, 'h-8 border-b border-border text-xs font-medium text-text-faint')}>
                        <span role="columnheader" aria-label="Status" />
                        <span role="columnheader">File</span>
                        <span role="columnheader" className="hidden text-right sm:block">Chunks</span>
                        <span role="columnheader" className="hidden text-right sm:block">Updated</span>
                        {host.canEdit && <span role="columnheader" aria-label="Actions" />}
                    </div>
                    {shown.map(d => {
                        const path = inBase(d.path, kb.folder);
                        const Icon = SOURCES.find(k => k.value === sourceKind(d))!.icon;
                        const status = STATUS[d.status] ?? { label: d.status, tone: 'neutral' as const };
                        return (
                            <div
                                key={d.id}
                                role="row"
                                className={cn(
                                    DOC_GRID, host.canEdit ? DOC_COLS_EDIT : DOC_COLS,
                                    'relative min-h-[50px] border-b border-border transition-colors hover:bg-row-hover',
                                    open === d.id && 'bg-row-hover',
                                    // The row draws the focus ring for its stretched control, as the runs list does.
                                    'has-[[data-row-open]:focus-visible]:outline has-[[data-row-open]:focus-visible]:outline-2',
                                    'has-[[data-row-open]:focus-visible]:-outline-offset-2 has-[[data-row-open]:focus-visible]:outline-accent',
                                )}
                            >
                                <span role="cell" className="flex items-center">
                                    <span aria-hidden className={cn('inline-block size-2 rounded-full', DOC_DOT[d.status] ?? 'bg-text-faint')} />
                                </span>
                                <span role="cell" className="min-w-0 py-2">
                                    <button
                                        type="button"
                                        data-row-open
                                        data-no-focus-ring
                                        onClick={() => setOpen(d.id)}
                                        aria-label={`Report for ${path}`}
                                        title={path}
                                        className="flex w-full min-w-0 items-center gap-2 text-left text-sm font-medium text-text after:absolute after:inset-0 after:content-['']"
                                    >
                                        <Icon className="size-3.5 shrink-0 text-text-faint" aria-hidden />
                                        <span className="truncate">{path}</span>
                                    </button>
                                    <span className="flex min-w-0 items-center gap-1.5 text-xs text-text-faint">
                                        <span className={cn('shrink-0', status.tone === 'danger' && 'font-medium text-danger')}>{status.label}</span>
                                        {d.status_reason && <span className="truncate">· {REASONS[d.status_reason] ?? d.status_reason}</span>}
                                        {d.source === 'web' && d.source_url && (
                                            <><span aria-hidden>·</span><WebLink url={d.source_url} className="relative z-10 min-w-0" /></>
                                        )}
                                    </span>
                                    {d.progress && WORKING.has(d.status) && (
                                        <DocProgress progress={d.progress} name={path} />
                                    )}
                                </span>
                                <span role="cell" className="hidden whitespace-nowrap text-right text-[13px] tabular-nums text-text-muted sm:block">{d.chunks ?? '—'}</span>
                                <span role="cell" className="hidden whitespace-nowrap text-right text-[13px] tabular-nums text-text-muted sm:block">{when(d.updated_at)}</span>
                                {host.canEdit && (
                                    <span role="cell" className="relative z-10 flex justify-end gap-0.5">
                                        <IconButton size="sm" label={`Retry ${path}`} icon={RotateCcw}
                                            disabled={!RESTING.has(d.status)} onClick={() => retry(d)} />
                                        <IconButton size="sm" tone="danger" label={`Delete ${path}`}
                                            icon={Trash2} disabled={d.status === 'deleting'} onClick={() => setRemoving(d)} />
                                    </span>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}
            </div>

            {host.webSources && kb.web.sources.length > 0 && (
                <WebSources kb={kb} crawls={crawls} notify={notify} onChanged={() => { refresh(); onChanged(); }} />
            )}
            <Costs kb={kb} docs={docs} />
            {host.evaluate && <Evaluate kb={kb} notify={notify} onScore={setHit5} />}

            <ReportDrawer
                doc={detail}
                open={!!open}
                folder={kb.folder}
                onClose={() => setOpen(null)}
                onRetry={host.canEdit ? d => retry(d) : undefined}
            />
            <ConfirmDialog
                open={!!removing}
                onClose={() => setRemoving(null)}
                onConfirm={remove}
                title={`Delete ${removing ? inBase(removing.path, kb.folder) : 'document'}?`}
                description={removing?.source === 'web'
                    ? "This deletes the fetched page. Its source's next crawl fetches it again, unless the source excludes it."
                    : 'This deletes the file from the Vault too; a file left there would be indexed again.'}
            />
            <AddWebDialog
                kb={kb}
                open={addingWeb}
                onClose={() => setAddingWeb(false)}
                onAdded={n => {
                    setAddingWeb(false);
                    notify(n === 1 ? 'Fetching it now. Its pages appear below as they arrive.'
                        : `Fetching ${n} sources now. Their pages appear below as they arrive.`);
                    refresh();
                    onChanged();
                }}
            />
            {editing && (
                <KnowledgeSettings
                    kb={kb}
                    documents={(kb.state.counts_by_status.indexed ?? 0) + (kb.state.counts_by_status.partial ?? 0)}
                    onClose={() => setEditing(false)}
                    onSaved={saved => {
                        setEditing(false);
                        notify(saved.reindex_started
                            ? 'Saved. The index is being rebuilt; the current one answers meanwhile.'
                            : 'Saved.');
                        onChanged();
                    }}
                />
            )}
        </section>
    );
}

const KINDS: { value: WebSourceKind; label: string; hint: string }[] = [
    { value: 'page', label: 'Pages', hint: 'Exactly these addresses, one page each.' },
    { value: 'sitemap', label: 'Sitemaps', hint: 'Every page a sitemap lists (sitemap.xml).' },
    { value: 'site', label: 'Sites', hint: 'Follows links from each address, on the same site and under its folder.' },
];

/** Add public web pages: each URL becomes a web source, fetched now and again on the base's `recrawl_days` (S1, F370). */
function AddWebDialog({
    kb, open, onClose, onAdded,
}: {
    kb: KnowledgeBase;
    open: boolean;
    onClose: () => void;
    onAdded: (accepted: number) => void;
}) {
    const [text, setText] = useState('');
    const [kind, setKind] = useState<WebSourceKind>('page');
    const [depth, setDepth] = useState('default');
    const [pages, setPages] = useState('');
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        if (!open) return;
        setText(''); setKind('page'); setDepth('default'); setPages(''); setError('');
    }, [open]);

    const urls = text.split(/\s+/).map(u => u.trim()).filter(Boolean);
    const add = async () => {
        setBusy(true);
        setError('');
        try {
            const accepted = await knowledgeApi.addUrls(kb.id, {
                urls, kind,
                ...(kind === 'site' && depth !== 'default' ? { max_depth: Number(depth) } : {}),
                ...(kind !== 'page' && pages ? { max_pages: Number(pages) } : {}),
            });
            onAdded(accepted.accepted);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    const depthOptions = useMemo(() => [
        { value: 'default', label: `The base's setting (${kb.web.max_depth})` },
        ...[0, 1, 2, 3, 4, 5].map(n => ({
            value: String(n),
            label: n === 0 ? '0 — only the address itself' : count(n, 'link') + ' away',
        })),
    ], [kb.web.max_depth]);

    return (
        <Modal
            open={open}
            onClose={onClose}
            title="Add web pages"
            description={`Public pages are fetched into this base, respecting each site's robots.txt, and ${kb.web.recrawl_days > 0
                ? `fetched again every ${count(kb.web.recrawl_days, 'day')}`
                : 'fetched again only when you ask'}.`}
            footer={
                <>
                    <Button variant="secondary" onClick={onClose} disabled={busy}>Cancel</Button>
                    <Button onClick={add} loading={busy} disabled={!urls.length}>
                        {urls.length > 1 ? `Add ${urls.length} addresses` : 'Add'}
                    </Button>
                </>
            }
        >
            <div className="space-y-4">
                {error && <ErrorNote>{error}</ErrorNote>}
                <Field label="Addresses" required hint="One per line, starting with https:// or http://.">
                    <Textarea
                        value={text}
                        onChange={e => setText(e.target.value)}
                        rows={4}
                        placeholder="https://docs.example.com/guide/"
                        spellCheck={false}
                        autoComplete="off"
                    />
                </Field>
                <Field label="What to fetch" hint={KINDS.find(k => k.value === kind)?.hint}>
                    <Select
                        value={kind}
                        onChange={setKind}
                        options={KINDS.map(k => ({ value: k.value, label: k.label, hint: k.hint }))}
                    />
                </Field>
                {kind === 'site' && (
                    <Field label="How far to follow links">
                        <Select value={depth} onChange={setDepth} options={depthOptions} />
                    </Field>
                )}
                {kind !== 'page' && (
                    <Field label="Most pages" hint={`At most this many pages per address; empty uses the base's ${kb.web.max_pages}.`}>
                        <Input
                            type="number"
                            inputMode="numeric"
                            min={1}
                            value={pages}
                            onChange={e => setPages(e.target.value)}
                            placeholder={String(kb.web.max_pages)}
                        />
                    </Field>
                )}
            </div>
        </Modal>
    );
}

function crawlLine(c: WebCrawlState): string {
    const n = c.counts;
    switch (c.status) {
        // Between the slices a runner runs a crawl in (cloud, F353): what it fetched so far.
        case 'queued': return n.fetched ? `Waiting for its turn · ${count(n.fetched, 'page')} fetched so far`
            : 'Waiting to fetch';
        // Its counts move as it works (F422): what it has fetched so far.
        case 'running': return n.fetched ? `Fetching now · ${count(n.fetched, 'page')} fetched` : 'Fetching now…';
        case 'interrupted': return 'Interrupted by a restart; fetched again shortly';
        case 'cancelled': return 'Stopped';
        case 'failed': return c.last_error ? `Failed: ${c.last_error}` : 'Failed';
        case 'idle': return 'Not fetched yet';
    }
    const changed = [n.written && `${n.written} new or changed`, n.deleted && `${n.deleted} removed`,
        n.blocked && `${n.blocked} disallowed`, n.failed && `${n.failed} failed`].filter(Boolean).join(', ');
    const head = `${count(c.pages, 'page')} · fetched ${when(c.finished_at)}`;
    // A crawl capped by its time or the vault says which (CCR-11); one capped by its page limit names none.
    const tail = c.status !== 'capped' ? ''
        : c.last_error ? ` — ${c.last_error}` : ' · stopped at the page limit, so nothing was removed';
    return `${head}${changed ? ` · ${changed}` : ''}${tail}`;
}

/** The base's web sources, each with its latest crawl: fetch again, or remove (and its pages with it). */
function WebSources({
    kb, crawls, notify, onChanged,
}: {
    kb: KnowledgeBase;
    crawls: WebCrawlState[];
    notify: (message: string, tone?: 'success' | 'warning' | 'danger') => void;
    onChanged: () => void;
}) {
    const { canEdit } = useKnowledgeHost();
    const [removing, setRemoving] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);
    const byId = new Map(crawls.map(c => [c.source_id, c]));
    const active = crawls.filter(c => CRAWLING.has(c.status)).length;
    const source = kb.web.sources.find(s => s.id === removing);

    const again = async (url: string, kind: WebSourceKind) => {
        try {
            await knowledgeApi.addUrls(kb.id, { urls: [url], kind });
            onChanged();
        } catch (e) {
            notify(e instanceof Error ? e.message : String(e), 'danger');
        }
    };

    const remove = async () => {
        if (!removing) return;
        setBusy(true);
        try {
            await knowledgeApi.updateBase(kb.id, {
                revision: kb.revision,
                web: { ...kb.web, sources: kb.web.sources.filter(s => s.id !== removing) },
            });
            notify('Source removed, with the pages only it fetched.');
            setRemoving(null);
            onChanged();
        } catch (e) {
            notify(e instanceof Error ? e.message : String(e), 'danger');
        } finally {
            setBusy(false);
        }
    };

    return (
        <Section
            variant="plain"
            title="Web sources"
            description={kb.web.recrawl_days > 0
                ? `Fetched again every ${count(kb.web.recrawl_days, 'day')}, respecting each site's robots.txt.`
                : "Fetched only when you ask, respecting each site's robots.txt."}
            summary={<Muted>{count(kb.web.sources.length, 'source')}{active ? ` · ${active} fetching` : ''}</Muted>}
            defaultOpen
        >
            <ul className="divide-y divide-border" aria-label="Web sources">
                {kb.web.sources.map(s => {
                    const c = byId.get(s.id);
                    return (
                        <li key={s.id} className="flex items-start gap-3 py-2.5">
                            <div className="min-w-0 flex-1 space-y-0.5">
                                <div className="flex min-w-0 items-center gap-2">
                                    <Badge size="sm" tone="neutral">{KINDS.find(k => k.value === s.kind)?.label.replace(/s$/, '') ?? s.kind}</Badge>
                                    <WebLink url={s.url} className="text-sm text-text" />
                                </div>
                                <p className={cn('text-xs', c?.status === 'failed' ? 'text-danger' : 'text-text-faint')} aria-live="polite">
                                    {c ? crawlLine(c) : 'Not fetched yet'}
                                </p>
                            </div>
                            {canEdit && (
                                <TableActions>
                                    <IconButton size="sm" label={`Fetch ${shortUrl(s.url)} again now`} icon={RotateCcw}
                                        disabled={!!c && CRAWLING.has(c.status)} onClick={() => again(s.url, s.kind)} />
                                    <IconButton size="sm" tone="danger" label={`Remove ${shortUrl(s.url)}`} icon={Trash2}
                                        onClick={() => setRemoving(s.id)} />
                                </TableActions>
                            )}
                        </li>
                    );
                })}
            </ul>
            <ConfirmDialog
                open={!!removing}
                onClose={() => setRemoving(null)}
                onConfirm={remove}
                busy={busy}
                confirmLabel="Remove"
                title={`Remove ${source ? shortUrl(source.url) : 'this source'}?`}
                description="Its crawl stops, and the pages only it fetched leave the base and the Vault. Pages another source also fetches stay."
            />
        </Section>
    );
}

/** A rebuild of the index after a change to how it is built: how far it has got, why it waits, or why it failed. */
function RebuildLine({ build, onRetry }: { build: NonNullable<KnowledgeBase['state']['build']>; onRetry?: () => void }) {
    if (build.status === 'failed') {
        return (
            <div role="status" className="flex flex-wrap items-start justify-between gap-3 rounded-md border border-danger/40 px-3 py-2 text-sm">
                <p className="min-w-0 text-text">
                    The rebuild failed; the current index still answers.
                    {build.error && <span className="mt-0.5 block break-words text-xs text-text-muted">{build.error}</span>}
                </p>
                {onRetry && (
                    <Button size="sm" variant="secondary" onClick={onRetry}>
                        <RotateCcw className="size-4" aria-hidden />Retry rebuild
                    </Button>
                )}
            </div>
        );
    }
    // Passages when the rebuild counts them (CCR-12), else documents: the bar moves within a long document too.
    const passages = build.passages_total ? { done: build.passages_done ?? 0, total: build.passages_total } : null;
    const wait = waitNote(build.waiting);
    const left = wait ? null : timeLeft(build.eta_s);
    const stage = build.stage ? PROGRESS_STAGE[build.stage] : null;
    const counted = [passages ? progressCount({ ...passages, unit: 'passages' }) : null, stage ? `now ${stage.toLowerCase()}` : null,
        left].filter(Boolean).join(' · ');
    return (
        <div role="status" className="rounded-md border border-border px-3 py-2 text-sm">
            <p className="text-text">
                {build.paused_reason
                    ? (BUILD_PAUSED[build.paused_reason] ?? `Paused: ${build.paused_reason}.`)
                    : `Rebuilding the index: ${build.docs_done} of ${count(build.docs_total, 'document')}. The current index answers meanwhile.`}
            </p>
            {!build.paused_reason && (
                <>
                    <ProgressBar
                        value={passages ? passages.done : build.docs_done}
                        max={passages ? passages.total : build.docs_total}
                        label="Rebuilding the index"
                        className="mt-2"
                    />
                    {counted && <p className="mt-1 text-xs text-text-muted">{counted}</p>}
                    {wait && <p className="mt-1 text-xs text-warning">{wait}</p>}
                </>
            )}
        </div>
    );
}

const GROUPS: { value: CostGroupBy; label: string }[] = [
    { value: 'stage', label: 'By stage' },
    { value: 'day', label: 'By day' },
    { value: 'document', label: 'By document' },
    { value: 'model', label: 'By model' },
];
const GROUP_HEADINGS: Record<CostGroupBy, string> = { stage: 'Stage', day: 'Day', document: 'Document', model: 'Model' };
const STAGE_NAMES: Record<string, string> = {
    embed: 'Embedding', rerank: 'Reranking', ocr: 'Reading scanned pages', parse: 'Reading files', enrich: 'Context',
    rewrite: 'Rewriting questions',
};

/** What the base has cost this month, grouped, against its budget. */
function Costs({ kb, docs }: { kb: KnowledgeBase; docs: KnowledgeDocument[] | null }) {
    const [groupBy, setGroupBy] = useState<CostGroupBy>('stage');
    const [groups, setGroups] = useState<CostGroup[] | null>(null);
    const [error, setError] = useState('');
    const spent = kb.state.month_spend_usd;
    const cap = kb.budget_monthly_usd;

    useEffect(() => {
        let live = true;
        knowledgeApi.costs(kb.id, groupBy)
            .then(g => { if (live) { setGroups(g); setError(''); } })
            .catch(e => { if (live) setError(e instanceof Error ? e.message : String(e)); });
        return () => { live = false; };
    }, [kb.id, groupBy, spent]);

    const paths = useMemo(() => new Map((docs ?? []).map(d => [d.id, inBase(d.path, kb.folder)])), [docs, kb.folder]);
    const name = (key: string) => {
        if (groupBy === 'stage') return STAGE_NAMES[key] ?? key;
        if (groupBy === 'document') return key === 'search' ? 'Searches' : (paths.get(key) ?? 'A deleted document');
        return key;
    };
    const money = formatUsd;

    return (
        <Section
            variant="plain"
            title="Costs"
            description="What this knowledge base has cost this month: indexing and searches."
            summary={<Muted>{money(spent)}{cap != null ? ` of ${money(cap)}` : ''} this month</Muted>}
        >
            {cap != null && (
                <div className="mb-3">
                    <div className="h-1.5 overflow-hidden rounded-full bg-surface-2" role="meter" aria-label="Budget used"
                        aria-valuemin={0} aria-valuemax={cap} aria-valuenow={Math.min(spent, cap)}>
                        <div className={cn('h-full', spent >= cap ? 'bg-danger' : 'bg-accent')}
                            style={{ width: `${cap ? Math.min(100, (spent / cap) * 100) : 100}%` }} />
                    </div>
                    <Muted className="mt-1 block text-xs">
                        {spent >= cap ? 'The monthly budget is reached: indexing waits and searches use words only.'
                            : `${money(cap - spent)} left of a ${money(cap)} monthly budget.`}
                    </Muted>
                </div>
            )}
            <div className="mb-2 flex justify-end">
                <Select size="sm" value={groupBy} onChange={setGroupBy} options={GROUPS} aria-label="Group costs" />
            </div>
            {error ? <ErrorNote>{error}</ErrorNote> : groups === null ? (
                <div className="flex justify-center py-4"><Spinner /></div>
            ) : groups.length === 0 ? (
                <Muted>Nothing has cost anything this month.</Muted>
            ) : (
                <Table density="compact" minWidth="24rem">
                    <THead>
                        <TR hover={false}>
                            <TH width="full">{GROUP_HEADINGS[groupBy]}</TH>
                            <TH width="min" align="right">Tokens</TH>
                            <TH width="min" align="right">Cost</TH>
                        </TR>
                    </THead>
                    <TBody>
                        {groups.map(g => (
                            <TR key={g.key}>
                                <TD><span className={cn('block truncate', groupBy !== 'stage' && 'font-mono text-xs')}>{name(g.key)}</span></TD>
                                <TD align="right" nowrap>{g.input_tokens ? g.input_tokens.toLocaleString() : g.units ? `${g.units} units` : '—'}</TD>
                                <TD align="right" nowrap>
                                    {money(g.est_usd)}
                                    {g.charged_microusd != null && <Muted> · charged {money(g.charged_microusd / 1_000_000)}</Muted>}
                                </TD>
                            </TR>
                        ))}
                    </TBody>
                </Table>
            )}
        </Section>
    );
}

/** Ask what an agent would, and see what it would find — and why (B1b). */
function TestSearch({ kb }: { kb: KnowledgeBase }) {
    const [query, setQuery] = useState('');
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<SearchResult | null>(null);
    const [error, setError] = useState<string | null>(null);

    const run = async (e: React.FormEvent) => {
        e.preventDefault();
        const q = query.trim();
        if (!q || busy) return;
        setBusy(true);
        setError(null);
        try {
            setResult(await knowledgeApi.search({ query: q, kb_ids: [kb.id], include_trace: true }));
        } catch (err) {
            setResult(null);
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setBusy(false);
        }
    };

    const trace = result?.trace;
    return (
        <Section variant="plain" defaultOpen title="Test search" description="Ask what an agent would ask, and see the passages it would get.">
            <form onSubmit={run} className="flex gap-2">
                <Input
                    type="search"
                    value={query}
                    onChange={e => setQuery(e.target.value)}
                    placeholder="e.g. When does the contract renew?"
                    aria-label={`Search ${kb.name}`}
                    autoComplete="off"
                    maxLength={2000}
                />
                <Button type="submit" variant="secondary" disabled={busy || !query.trim()}>
                    {busy ? <Spinner /> : <Search className="size-4" aria-hidden />}Search
                </Button>
            </form>
            {error && <ErrorNote className="mt-3">{error}</ErrorNote>}
            {result && (
                <div className="mt-3 space-y-3" aria-live="polite">
                    <Muted>{result.hits.length ? count(result.hits.length, 'passage') : 'Nothing matched.'}</Muted>
                    {result.degraded.length > 0 && (
                        <ul className="space-y-0.5 text-xs text-text-muted">
                            {result.degraded.map(code => <li key={code}>{DEGRADED[code] ?? code}</li>)}
                        </ul>
                    )}
                    {result.hits.length > 0 && (
                        <ol className="space-y-3">
                            {result.hits.map(hit => {
                                const c = hit.citation;
                                const pages = c.page_start
                                    ? (c.page_end && c.page_end !== c.page_start ? `pp. ${c.page_start}–${c.page_end}` : `p. ${c.page_start}`)
                                    : '';
                                const where = [inBase(c.path, kb.folder), pages, (c.heading_path || []).join(' › ')]
                                    .filter(Boolean).join(' · ');
                                return (
                                    <li key={hit.chunk_id} className="flex gap-3 text-sm">
                                        <span className="inline-grid h-[18px] min-w-[22px] shrink-0 place-items-center rounded-[5px] bg-accent-subtle px-1.5 text-[11px] font-semibold text-accent-ink" translate="no">{hit.label}</span>
                                        <div className="min-w-0 flex-1 space-y-1">
                                            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                                                <span className="min-w-0 truncate font-mono text-xs text-text">{where}</span>
                                                <Muted className="tabular-nums">score {hit.score.toFixed(3)}</Muted>
                                            </div>
                                            <p className="line-clamp-3 whitespace-pre-line text-text-muted">{hit.content}</p>
                                        </div>
                                    </li>
                                );
                            })}
                        </ol>
                    )}
                    {trace && (
                        <details className="rounded-md border border-border px-3 py-2 text-xs">
                            <summary className="cursor-pointer select-none text-text-muted">
                                Trace · <span className="font-mono" translate="no">{trace.query_id}</span>
                                {trace.timings_ms.total != null && ` · ${trace.timings_ms.total} ms`}
                            </summary>
                            <RetrievalTrace trace={trace} className="mt-2" />
                        </details>
                    )}
                </div>
            )}
        </Section>
    );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <div className="flex justify-between gap-4 py-1 text-sm">
            <dt className="text-text-muted">{label}</dt>
            <dd className="min-w-0 truncate text-right text-text">{children}</dd>
        </div>
    );
}

const STAGE_TONE = { ok: 'success', skipped: 'neutral', partial: 'warning', failed: 'danger' } as const;

function ReportDrawer({
    doc, open, folder, onClose, onRetry,
}: {
    doc: KnowledgeDocumentDetail | null;
    open: boolean;
    folder: string;
    onClose: () => void;
    /** Absent for someone who may not retry: the drawer only reports. */
    onRetry?: (doc: KnowledgeDocument) => void;
}) {
    const report = doc?.report ?? null;
    const canRetry = !!doc && RESTING.has(doc.status) && (report?.retry.allowed ?? true);
    return (
        <Drawer
            open={open}
            onClose={onClose}
            title={doc ? inBase(doc.path, folder) : 'Document'}
            description={doc ? <StatusBadge status={doc.status} /> : undefined}
            footer={doc && (
                <>
                    <Button variant="secondary" onClick={onClose}>Close</Button>
                    {onRetry && (
                        <Button onClick={() => onRetry(doc)} disabled={!canRetry}>
                            <RotateCcw className="size-4" aria-hidden />Retry
                        </Button>
                    )}
                </>
            )}
        >
            {!doc ? (
                <div className="flex justify-center py-8"><Spinner /></div>
            ) : (
                <div className="space-y-6">
                    {(doc.status_reason || doc.error || report?.retry.hint) && (
                        <div className="space-y-1.5">
                            {doc.status_reason && (
                                <p className="text-sm font-medium text-text">{REASONS[doc.status_reason] ?? doc.status_reason}</p>
                            )}
                            {doc.error && <p className="whitespace-pre-wrap break-words text-sm text-text-muted">{doc.error}</p>}
                            {report?.retry.hint && <p className="text-sm text-text-muted">{report.retry.hint}</p>}
                        </div>
                    )}

                    <dl className="divide-y divide-border">
                        <Fact label="Source">
                            {doc.source === 'web' && doc.source_url ? <WebLink url={doc.source_url} /> : doc.source}
                        </Fact>
                        <Fact label="Size">{formatBytes(doc.size_bytes)}</Fact>
                        <Fact label="Type"><Mono>{doc.mime ?? '—'}</Mono></Fact>
                        {doc.title && <Fact label="Title">{doc.title}</Fact>}
                        <Fact label="Chunks">{doc.chunks ?? '—'}</Fact>
                        {doc.pages ? <Fact label="Pages">{doc.pages}</Fact> : null}
                        <Fact label="Version">{doc.version}</Fact>
                        <Fact label="Indexed">{when(doc.indexed_at)}</Fact>
                        {report?.parser && <Fact label="Read by"><Mono>{report.parser.name}/{report.parser.version}</Mono></Fact>}
                        {report && report.cost.est_usd > 0 && <Fact label="Cost">${report.cost.est_usd.toFixed(5)}</Fact>}
                    </dl>

                    {report && report.stages.length > 0 && (
                        <div>
                            <Label className="mb-2 block">What happened · attempt {report.attempt}</Label>
                            <ol className="space-y-2">
                                {report.stages.map((s, i) => (
                                    <li key={`${s.stage}-${i}`} className="rounded-md border border-border px-3 py-2">
                                        <div className="flex items-center justify-between gap-3">
                                            <span className="text-sm capitalize text-text">{s.stage}</span>
                                            <span className="flex items-center gap-2">
                                                <Muted>{s.duration_ms} ms</Muted>
                                                <Badge size="sm" tone={STAGE_TONE[s.status]}>{s.status}</Badge>
                                            </span>
                                        </div>
                                        {s.error && (
                                            <p className="mt-1 break-words text-xs text-danger">{s.error.message}</p>
                                        )}
                                    </li>
                                ))}
                            </ol>
                        </div>
                    )}

                    {report && report.warnings.length > 0 && (
                        <div>
                            <Label className="mb-2 block">Warnings</Label>
                            <ul className="space-y-1.5">
                                {report.warnings.map((w, i) => (
                                    <li key={`${w.code}-${i}`} className="text-sm text-text-muted">
                                        <Mono>{w.code}</Mono> — {w.message}
                                    </li>
                                ))}
                            </ul>
                        </div>
                    )}
                </div>
            )}
        </Drawer>
    );
}
