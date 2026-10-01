'use client';

/*
 * Knowledge: a Vault folder made searchable (D157).
 *
 * The left column lists the knowledge bases; the right shows the selected one's
 * documents — every file in its folder, with where it stands — and takes
 * uploads. A document opens a drawer with its ingestion report: what happened
 * to it, stage by stage, and whether retrying can help.
 *
 * Polls every 2 s while anything is still being worked on, else every 10 s, as
 * the Repos screen does.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FolderOpen, Plus, RotateCcw, Search, Trash2, Upload } from 'lucide-react';

import {
    Badge, Button, ConfirmDialog, Drawer, EmptyState, ErrorNote, Field, IconButton, Input, Label, Modal, Mono,
    Muted, Screen, Section, Select, Spinner, TBody, TD, TH, THead, TR, Table, TableActions, Textarea, Toast, useToast,
    type SelectOption,
} from '@/components/ui';
import { count } from '@/lib/copy';
import { cn } from '@/lib/cn';
import {
    ACCEPTED, DEGRADED, REASONS, RESTING, STATUS, WORKING, formatBytes, inBase, knowledgeApi, uploadToKnowledge,
    useKnowledgeEnabled,
    type DocStatus, type KnowledgeBase, type KnowledgeDocument, type KnowledgeDocumentDetail, type SearchResult,
} from '@/lib/knowledge';

type VaultNode = { name: string; path: string; type: 'file' | 'folder'; children?: VaultNode[] };

function folders(nodes: VaultNode[], out: string[] = []): string[] {
    for (const n of nodes) {
        if (n.type !== 'folder') continue;
        out.push(n.path);
        folders(n.children ?? [], out);
    }
    return out;
}

function when(iso: string | null | undefined): string {
    return iso ? new Date(iso).toLocaleString() : '—';
}

function StatusBadge({ status }: { status: DocStatus }) {
    const s = STATUS[status] ?? { label: status, tone: 'neutral' as const };
    return <Badge tone={s.tone} dot size="sm">{s.label}</Badge>;
}

function summary(kb: KnowledgeBase): string {
    const c = kb.state.counts_by_status;
    const total = Object.values(c).reduce((a, b) => a + (b ?? 0), 0);
    if (!total) return 'No documents yet';
    const parts = [count(total, 'document')];
    const failed = (c.failed ?? 0) + (c.blocked ?? 0);
    const working = [...WORKING].reduce((a, s) => a + (c[s] ?? 0), 0);
    if (working) parts.push(`${working} in progress`);
    if (failed) parts.push(`${failed} failed`);
    return parts.join(' · ');
}

export function KnowledgeScreen() {
    const enabled = useKnowledgeEnabled();
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
        return <Screen title="Knowledge"><div className="flex justify-center py-12"><Spinner /></div></Screen>;
    }
    if (!enabled) {
        return (
            <Screen title="Knowledge" description="Agents that answer from your documents, with sources.">
                <EmptyState title="Knowledge is off in this workspace">
                    Turn on <span className="font-mono">knowledge_enabled</span> in the workspace settings to make
                    Vault folders searchable.
                </EmptyState>
            </Screen>
        );
    }

    return (
        <Screen
            title="Knowledge"
            description="Vault folders made searchable. Every file in a knowledge base's folder is read and indexed."
            width="wide"
            actions={<Button onClick={() => setCreating(true)}><Plus className="size-4" aria-hidden />New knowledge base</Button>}
        >
            {error && <ErrorNote className="mb-4">{error}</ErrorNote>}
            {bases === null ? (
                <div className="flex justify-center py-12"><Spinner /></div>
            ) : bases.length === 0 ? (
                <EmptyState
                    title="No knowledge bases yet"
                    action={<Button onClick={() => setCreating(true)}>New knowledge base</Button>}
                >
                    A knowledge base is a Vault folder. Pick one, or name a new one, and everything in it is indexed.
                </EmptyState>
            ) : (
                <div className="grid gap-6 lg:grid-cols-[17rem_1fr]">
                    <nav aria-label="Knowledge bases" className="space-y-1">
                        {bases.map(b => (
                            <button
                                key={b.id}
                                type="button"
                                onClick={() => setSelected(b.id)}
                                aria-current={b.id === selected ? 'true' : undefined}
                                className={cn(
                                    'w-full rounded-md border px-3 py-2.5 text-left transition-colors',
                                    b.id === selected
                                        ? 'border-accent bg-accent-subtle/40'
                                        : 'border-border hover:bg-surface-2',
                                )}
                            >
                                <span className="block truncate text-sm font-medium text-text">{b.name}</span>
                                <span className="block truncate font-mono text-xs text-text-muted">{b.folder}</span>
                                <span className="mt-1 block text-xs text-text-faint">
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
    const [known, setKnown] = useState<string[]>([]);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    useEffect(() => {
        if (!open) return;
        setName(''); setFolder(''); setDescription(''); setError('');
        fetch('/api/vault/tree')
            .then(r => r.json())
            .then(data => setKnown(folders(data?.tree ?? []).filter(p => !/^(tool_outputs|playwright|compaction_archives)(\/|$)/.test(p))))
            .catch(() => setKnown([]));
    }, [open]);

    const create = async () => {
        setBusy(true);
        setError('');
        try {
            onCreated(await knowledgeApi.createBase({ name: name.trim(), folder: folder.trim(), description }));
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
            </div>
        </Modal>
    );
}

type Uploading = { name: string; sent: number; total: number; error?: string };

function BaseDetail({
    kb, onChanged, onDelete, notify,
}: {
    kb: KnowledgeBase;
    onChanged: () => void;
    onDelete: () => void;
    notify: (message: string, tone?: 'success' | 'warning' | 'danger') => void;
}) {
    const [docs, setDocs] = useState<KnowledgeDocument[] | null>(null);
    const [tick, setTick] = useState(0);
    const [filter, setFilter] = useState<'all' | DocStatus>('all');
    const [open, setOpen] = useState<string | null>(null);
    const [fetched, setFetched] = useState<KnowledgeDocumentDetail | null>(null);
    const detail = fetched && fetched.id === open ? fetched : null;
    const [removing, setRemoving] = useState<KnowledgeDocument | null>(null);
    const [uploads, setUploads] = useState<Uploading[]>([]);
    const [dragging, setDragging] = useState(false);
    const input = useRef<HTMLInputElement>(null);

    const refresh = useCallback(() => setTick(t => t + 1), []);

    useEffect(() => {
        let live = true;
        knowledgeApi.documents(kb.id)
            .then(list => { if (live) setDocs(list); })
            .catch(e => notify(e instanceof Error ? e.message : String(e), 'danger'));
        return () => { live = false; };
    }, [kb.id, tick, notify]);

    const working = !!docs?.some(d => WORKING.has(d.status)) || uploads.some(u => !u.error && u.sent < u.total);
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
        const list = [...files];
        if (!list.length) return;
        setUploads(u => [...u, ...list.map(f => ({ name: f.name, sent: 0, total: f.size }))]);
        for (const file of list) {
            try {
                await uploadToKnowledge(kb.id, file, {
                    onProgress: (sent, total) => setUploads(u => u.map(x => (x.name === file.name ? { ...x, sent, total } : x))),
                });
                setUploads(u => u.filter(x => x.name !== file.name));
            } catch (e) {
                const message = e instanceof Error ? e.message : String(e);
                setUploads(u => u.map(x => (x.name === file.name ? { ...x, error: message } : x)));
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
    const shown = (docs ?? [])
        .filter(d => filter === 'all' || d.status === filter)
        .sort((a, b) => a.path.localeCompare(b.path));

    return (
        <section aria-label={kb.name} className="min-w-0 space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                    <h2 className="truncate text-base font-semibold text-text">{kb.name}</h2>
                    <p className="mt-0.5 flex items-center gap-1.5 text-sm text-text-muted">
                        <FolderOpen className="size-3.5 shrink-0" aria-hidden />
                        <Mono>{kb.folder}</Mono>
                        {kb.state.month_spend_usd > 0 && <Muted>· ${kb.state.month_spend_usd.toFixed(4)} this month</Muted>}
                    </p>
                    {kb.description && <p className="mt-1 text-sm text-text-muted">{kb.description}</p>}
                </div>
                <div className="flex gap-2">
                    <Button variant="secondary" onClick={() => input.current?.click()}>
                        <Upload className="size-4" aria-hidden />Upload files
                    </Button>
                    <Button variant="ghost" onClick={onDelete}>
                        <Trash2 className="size-4" aria-hidden />Delete
                    </Button>
                </div>
            </div>

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
                    'rounded-lg border border-dashed px-4 py-3 text-sm text-text-muted transition-colors',
                    dragging ? 'border-accent bg-accent-subtle/30' : 'border-border',
                )}
            >
                Drop files here to add them to <span className="font-mono">{kb.folder}</span>. Documents, spreadsheets,
                slides, email, web pages and code are read; scanned pages and images are read once OCR is configured.
            </div>
            {uploads.length > 0 && (
                <ul className="space-y-1" aria-label="Uploads">
                    {uploads.map(u => (
                        <li key={u.name} className="flex items-center gap-2 text-sm">
                            <span className="min-w-0 flex-1 truncate">{u.name}</span>
                            {u.error ? (
                                <>
                                    <span className="text-danger">{u.error}</span>
                                    <IconButton size="sm" label={`Dismiss ${u.name}`} icon={Trash2}
                                        onClick={() => setUploads(x => x.filter(y => y.name !== u.name))} />
                                </>
                            ) : (
                                <Muted>{u.total ? Math.round((u.sent / u.total) * 100) : 0}%</Muted>
                            )}
                        </li>
                    ))}
                </ul>
            )}

            <TestSearch kb={kb} />

            <div className="flex items-center justify-between gap-3">
                <Label>{docs ? count(shown.length, 'document') : 'Documents'}</Label>
                <Select size="sm" value={filter} onChange={setFilter} options={statuses} aria-label="Filter by status" />
            </div>

            {docs === null ? (
                <div className="flex justify-center py-8"><Spinner /></div>
            ) : shown.length === 0 ? (
                <EmptyState title={docs.length ? 'No documents with this status' : 'No documents yet'}>
                    {docs.length ? null : 'Upload files, or add them to the folder in the Vault.'}
                </EmptyState>
            ) : (
                <Table density="compact" minWidth="36rem">
                    <THead>
                        <TR hover={false}>
                            <TH width="full">File</TH>
                            <TH width="min">Status</TH>
                            <TH width="min" align="right">Chunks</TH>
                            <TH width="min">Updated</TH>
                            <TH width="min"><span className="sr-only">Actions</span></TH>
                        </TR>
                    </THead>
                    <TBody>
                        {shown.map(d => (
                            <TR key={d.id} selected={open === d.id}>
                                <TD>
                                    <button
                                        type="button"
                                        onClick={() => setOpen(d.id)}
                                        aria-label={`Report for ${inBase(d.path, kb.folder)}`}
                                        className="block max-w-full truncate text-left font-mono text-xs text-text hover:underline"
                                    >
                                        {inBase(d.path, kb.folder)}
                                    </button>
                                    {d.status_reason && (
                                        <span className="block truncate text-xs text-text-faint">
                                            {REASONS[d.status_reason] ?? d.status_reason}
                                        </span>
                                    )}
                                </TD>
                                <TD nowrap><StatusBadge status={d.status} /></TD>
                                <TD align="right" nowrap>{d.chunks ?? '—'}</TD>
                                <TD nowrap><Muted>{when(d.updated_at)}</Muted></TD>
                                <TD nowrap>
                                    <TableActions>
                                        <IconButton size="sm" label={`Retry ${inBase(d.path, kb.folder)}`} icon={RotateCcw}
                                            disabled={!RESTING.has(d.status)} onClick={() => retry(d)} />
                                        <IconButton size="sm" tone="danger" label={`Delete ${inBase(d.path, kb.folder)}`}
                                            icon={Trash2} disabled={d.status === 'deleting'} onClick={() => setRemoving(d)} />
                                    </TableActions>
                                </TD>
                            </TR>
                        ))}
                    </TBody>
                </Table>
            )}

            <ReportDrawer
                doc={detail}
                open={!!open}
                folder={kb.folder}
                onClose={() => setOpen(null)}
                onRetry={d => retry(d)}
            />
            <ConfirmDialog
                open={!!removing}
                onClose={() => setRemoving(null)}
                onConfirm={remove}
                title={`Delete ${removing ? inBase(removing.path, kb.folder) : 'document'}?`}
                description="This deletes the file from the Vault too; a file left there would be indexed again."
            />
        </section>
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
        <Section title="Test search" description="Ask what an agent would ask, and see the passages it would get.">
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
                                        <span className="font-mono text-xs text-text-faint" translate="no">{hit.label}</span>
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
                            <dl className="mt-2 space-y-1">
                                {trace.legs.map((leg, i) => (
                                    <Fact key={`leg-${i}`} label={`Leg: ${leg.leg}`}>
                                        {leg.returned} of {leg.requested}{leg.filtered ? ', filtered in the index' : ''}
                                        {` · ${leg.latency_ms} ms`}
                                    </Fact>
                                ))}
                                {trace.cuts.map((cut, i) => (
                                    <Fact key={`cut-${i}`} label={`Cut: ${cut.stage}`}>
                                        {cut.dropped_count} dropped{cut.rule ? ` (${cut.rule})` : ''}
                                    </Fact>
                                ))}
                                {trace.legs.length === 0 && trace.cuts.length === 0 && <Muted>No legs ran.</Muted>}
                            </dl>
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
    onRetry: (doc: KnowledgeDocument) => void;
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
                    <Button onClick={() => onRetry(doc)} disabled={!canRetry}>
                        <RotateCcw className="size-4" aria-hidden />Retry
                    </Button>
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
                        <Fact label="Source">{doc.source}</Fact>
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
