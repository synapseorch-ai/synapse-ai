import React, { useState, useEffect, useCallback } from 'react';
import { Plus, Trash2, RefreshCw, ChevronDown, ChevronUp, Info, Square } from 'lucide-react';
import { ConfirmationModal } from './ConfirmationModal';
import { ToastNotification } from './ToastNotification';
import { FIELD_LABEL, FORM } from './form';

import { Badge, Button, EmptyState, ErrorNote, IconButton, Input, Section, Spinner, Textarea, type BadgeTone } from '@/components/ui';

/** The stop glyph, filled — the kit's `IconButton` takes a component, not an element. */
const StopIcon = ({ className }: { className?: string }) => <Square className={`${className ?? ''} fill-current`} />;

/** A status word in sentence case ("not indexed" → "Not indexed"); the value itself is unchanged. */
const sentence = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
// Mirrors backend's BASE_EXCLUDED_PATTERNS — shown read-only in the form.
const BASE_EXCLUDED = [
    '**/venv/**', '**/.venv/**', '**/env/**', '**/__pycache__/**', '**/site-packages/**',
    '**/*.pyc', '**/*.pyo', '**/.eggs/**', '**/*.egg-info/**',
    '**/.pytest_cache/**', '**/.tox/**', '**/.mypy_cache/**',
    '**/node_modules/**', '**/.next/**', '**/.nuxt/**', '**/dist/**', '**/build/**',
    '**/.cache/**', '**/.turbo/**',
    '**/vendor/**', '**/.bundle/**', '**/Pods/**', '**/bower_components/**',
    '**/.git/**', '**/.svn/**', '**/.hg/**',
    '**/.idea/**', '**/.vscode/**', '**/.vs/**',
    '**/target/**', '**/*.min.js', '**/*.min.css', '**/*.map',
    '**/*.class', '**/*.o', '**/*.so', '**/*.jar',
    '**/coverage/**', '**/htmlcov/**', '**/__snapshots__/**',
    '**/*.lock', '**/*.log', '**/*.tmp', '**/.DS_Store',
];

export interface Repo {
    id: string;
    name: string;
    path: string;
    description: string;
    included_patterns: string[];
    excluded_patterns: string[];
    last_indexed: string | null;
    status: string;
    file_count: number;
    error_message?: string;
}

interface ReposTabProps {
    embeddingModel?: string;
    embedCode?: boolean;
}

export function ReposTab({ embeddingModel, embedCode }: ReposTabProps) {
    const [repos, setRepos] = useState<Repo[]>([]);
    const [isLoading, setIsLoading] = useState(true);
    const [draftRepo, setDraftRepo] = useState<Partial<Repo> | null>(null);
    const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
    const [reindexingIds, setReindexingIds] = useState<Set<string>>(new Set());
    const [stoppingIds, setStoppingIds] = useState<Set<string>>(new Set());
    const [showBasePatterns, setShowBasePatterns] = useState(false);
    const [excludedText, setExcludedText] = useState('');
    const [includedText, setIncludedText] = useState('');
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'warning' | 'error' } | null>(null);

    const showToast = (message: string, type: 'success' | 'warning' | 'error' = 'success') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), type === 'error' ? 8000 : 4000);
    };

    const fetchRepos = useCallback(async () => {
        try {
            const res = await fetch('/api/repos');
            if (res.ok) {
                const data: Repo[] = await res.json();
                setRepos(data);
                setReindexingIds(prev => {
                    const next = new Set(prev);
                    for (const id of prev) {
                        const repo = data.find(r => r.id === id);
                        if (!repo || repo.status !== 'indexing') next.delete(id);
                    }
                    return next;
                });
                setStoppingIds(prev => {
                    const next = new Set(prev);
                    for (const id of prev) {
                        const repo = data.find(r => r.id === id);
                        if (!repo || !['indexing', 'stopping'].includes(repo.status)) next.delete(id);
                    }
                    return next;
                });
            }
        } catch (error) {
            console.error('Failed to fetch repos', error);
        } finally {
            setIsLoading(false);
        }
    }, []);

    // Sync textarea state when the draft changes
    useEffect(() => {
        if (draftRepo) {
            setExcludedText((draftRepo.excluded_patterns || ['.*']).join('\n'));
            setIncludedText((draftRepo.included_patterns || []).join('\n'));
        }
    }, [draftRepo?.id]);

    useEffect(() => {
        fetchRepos();
    }, [fetchRepos]);

    // Poll 2 s while any repo is indexing so the chunk counter feels live;
    // else every 6 s to reduce idle load.
    useEffect(() => {
        const activelyIndexing =
            repos.some(r => r.status === 'indexing') || reindexingIds.size > 0;
        const interval = setInterval(fetchRepos, activelyIndexing ? 2000 : 6000);
        return () => clearInterval(interval);
    }, [repos, reindexingIds, fetchRepos]);

    const handleSaveRepo = async () => {
        if (!draftRepo?.name || !draftRepo?.path) {
            showToast('Name and Path are required', 'warning');
            return;
        }
        setIsSaving(true);
        const parseLines = (text: string) =>
            text.split('\n').map(s => s.trim()).filter(Boolean);

        const excluded = parseLines(excludedText).length
            ? parseLines(excludedText)
            : ['.*'];
        const included = parseLines(includedText).length
            ? parseLines(includedText)
            : ['*.py', '*.ts', '*.tsx', '*.js', '*.jsx', '*.rs', '*.go',
               '*.java', '*.md', '*.html', '*.vue', '*.css', '*.scss', '*.cpp', '*.c'];

        const newRepo = {
            id: draftRepo.id || 'repo_' + Date.now(),
            name: draftRepo.name,
            path: draftRepo.path,
            description: draftRepo.description || '',
            included_patterns: included,
            excluded_patterns: excluded,
            status: draftRepo.status || 'pending',
            file_count: draftRepo.file_count || 0,
        };
        try {
            const res = await fetch('/api/repos', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newRepo),
            });
            if (res.ok) {
                showToast(draftRepo.id ? 'Repo updated!' : 'Repo added!', 'success');
                setDraftRepo(null);
                fetchRepos();
            } else {
                showToast('Failed to save repo', 'error');
            }
        } catch {
            showToast('Failed to save repo', 'error');
        } finally {
            setIsSaving(false);
        }
    };

    const handleDeleteRepo = async (id: string) => {
        try {
            const res = await fetch(`/api/repos/${id}`, { method: 'DELETE' });
            if (res.ok) {
                fetchRepos();
            } else {
                showToast('Failed to delete repo', 'error');
            }
        } catch {
            showToast('Failed to delete repo', 'error');
        }
    };

    const handleReindex = async (id: string, e: React.MouseEvent) => {
        e.stopPropagation();
        const repo = repos.find(r => r.id === id);
        // Guard: block if already indexing (server returns 409 anyway)
        if (repo?.status === 'indexing' || reindexingIds.has(id)) return;

        setReindexingIds(prev => new Set(prev).add(id));
        try {
            const res = await fetch(`/api/repos/${id}/reindex`, { method: 'POST' });
            if (res.ok || res.status === 409) {
                // 409 = already indexing — backend refused, just refresh silently
                fetchRepos();
            } else {
                const detail = await res.json().catch(() => ({ detail: res.statusText }));
                showToast(`Re-index failed: ${detail?.detail ?? res.statusText}`, 'error');
                setReindexingIds(prev => { const n = new Set(prev); n.delete(id); return n; });
                fetchRepos(); // Refresh to show error_message in the repo card
            }
        } catch {
            showToast('Re-index request failed', 'error');
            setReindexingIds(prev => { const n = new Set(prev); n.delete(id); return n; });
        }
    };

    const handleStop = async (id: string, e: React.MouseEvent) => {
        e.stopPropagation();
        setStoppingIds(prev => new Set(prev).add(id));
        try {
            await fetch(`/api/repos/${id}/stop-index`, { method: 'POST' });
            fetchRepos();
        } catch {
            setStoppingIds(prev => { const n = new Set(prev); n.delete(id); return n; });
        }
    };

    // Quiet status (UX1): success is a dot, a failure is red, the rest neutral.
    const getStatusTone = (status: string): BadgeTone => {
        switch (status) {
            case 'indexed':  return 'success';
            case 'indexing': return 'machine';
            case 'stopping': return 'warning';
            case 'error':    return 'danger';
            default:         return 'neutral';
        }
    };

    if (isLoading) return <div className="p-8 text-center text-text-faint">Loading…</div>;

    /* ── Form view ─────────────────────────────────────────────────────── */
    if (draftRepo !== null) {
        return (
            <Section
                variant="plain"
                collapsible={false}
                title={draftRepo.id ? 'Edit repo' : 'Add new repo'}
                description="Configure codebase indexing settings."
            >
                {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
                <div className={`${FORM} space-y-4`}>
                    {embedCode && !embeddingModel && (
                        <div className="flex items-start gap-2 rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5 text-xs leading-relaxed text-text">
                            <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                            <span>
                                No embedding model configured. Indexing requires an embedding model.{' '}
                                <a href="/settings/models" className="text-accent-ink underline underline-offset-2">
                                    Go to Models settings →
                                </a>
                            </span>
                        </div>
                    )}

                    <div className="space-y-1.5">
                        <label htmlFor="repos-repo-name" className={FIELD_LABEL}>Repo name</label>
                        <Input
                            id="repos-repo-name"
                            type="text"
                            value={draftRepo.name || ''}
                            onChange={e => setDraftRepo({ ...draftRepo, name: e.target.value })}
                            placeholder="e.g. Frontend App"
                            autoComplete="off"
                        />
                    </div>
                    <div className="space-y-1.5">
                        <label htmlFor="repos-absolute-directory-path" className={FIELD_LABEL}>Absolute directory path</label>
                        <Input
                            id="repos-absolute-directory-path"
                            type="text"
                            value={draftRepo.path || ''}
                            onChange={e => setDraftRepo({ ...draftRepo, path: e.target.value })}
                            className="font-code"
                            placeholder="/home/user/projects/app"
                            autoComplete="off"
                        />
                    </div>
                    <div className="space-y-1.5">
                        <label htmlFor="repos-interconnection-description" className={FIELD_LABEL}>Interconnection description</label>
                        <Textarea
                            id="repos-interconnection-description"
                            value={draftRepo.description || ''}
                            onChange={e => setDraftRepo({ ...draftRepo, description: e.target.value })}
                            className="min-h-[80px]"
                            placeholder="Help the LLM understand what this repo contains…"
                        />
                    </div>

                    {/* ── Advanced Settings Toggle ─────────────────────── */}
                    <div>
                        <button
                            type="button"
                            onClick={() => setShowAdvanced(p => !p)}
                            className="group flex items-center gap-1.5 rounded-sm text-xs text-text-muted transition-colors hover:text-text"
                        >
                            {showAdvanced ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                            <span className="underline underline-offset-2 decoration-dotted group-hover:decoration-solid transition-all">
                                Advanced settings
                            </span>
                        </button>
                    </div>

                    {showAdvanced && (
                        <div className="space-y-4 border-t border-border pt-4">
                            {/* ── Excluded Patterns ───────────────────────────────── */}
                            <div className="space-y-2">
                                <div className="flex items-center gap-2">
                                    <label className={FIELD_LABEL}>Extra excluded patterns</label>
                                    <Badge size="sm" className="font-code">one per line</Badge>
                                </div>

                                {/* Pattern-matching explainer */}
                                <div className="flex gap-2 rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5 text-xs leading-relaxed text-text">
                                    <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                                    <div className="space-y-1">
                                        <p><span className="font-code font-medium">data</span> → excludes <em>every</em> directory named <span className="font-code">data</span> (backend/data AND frontend/data)</p>
                                        <p><span className="font-code font-medium">backend/data</span> → excludes <em>only</em> <span className="font-code">backend/data</span>, leaving <span className="font-code">frontend/data</span> indexed</p>
                                        <p><span className="font-code font-medium">*.json</span> → excludes all JSON files anywhere in the tree</p>
                                    </div>
                                </div>

                                <Textarea
                                    value={excludedText}
                                    onChange={e => setExcludedText(e.target.value)}
                                    className="min-h-[80px] font-code"
                                    placeholder={`.*\nbackend/data\nfixtures`}
                                />

                                {/* Collapsible base exclusions */}
                                <button
                                    type="button"
                                    onClick={() => setShowBasePatterns(p => !p)}
                                    className="flex items-center gap-1.5 rounded-sm text-xs text-text-muted transition-colors hover:text-text"
                                >
                                    {showBasePatterns ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                                    Always excluded automatically ({BASE_EXCLUDED.length} base patterns)
                                </button>
                                {showBasePatterns && (
                                    <div className="flex flex-wrap gap-1 rounded-md bg-surface-2 p-3">
                                        {BASE_EXCLUDED.map(p => (
                                            <span key={p} className="rounded-sm bg-surface px-1.5 py-0.5 font-code text-[0.6875rem] text-text-muted">{p}</span>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {/* ── Included Patterns ───────────────────────────────── */}
                            <div className="space-y-2">
                                <label htmlFor="repos-included-file-types" className={FIELD_LABEL}>Included file types</label>
                                <Textarea
                                    id="repos-included-file-types"
                                    value={includedText}
                                    onChange={e => setIncludedText(e.target.value)}
                                    className="min-h-[60px] font-code"
                                    placeholder={`*.py\n*.ts\n*.tsx\n*.md`}
                                />
                                <p className="text-xs text-text-faint">Only files matching these globs will be indexed. Leave blank to use defaults.</p>
                            </div>
                        </div>
                    )}

                    <div className="flex justify-end gap-2 pt-2">
                        <Button variant="ghost" onClick={() => setDraftRepo(null)}>
                            Cancel
                        </Button>
                        <Button onClick={handleSaveRepo} disabled={isSaving} loading={isSaving}>
                            {isSaving ? 'Saving…' : 'Save repository'}
                        </Button>
                    </div>
                </div>
            </Section>
        );
    }

    /* ── List view ─────────────────────────────────────────────────────── */
    return (
        <Section
            variant="plain"
            collapsible={false}
            title="Code repositories"
            description="Manage your agent's codebases for semantic code searching."
            actions={
                <Button size="sm" onClick={() => setDraftRepo({})}>
                    <Plus className="h-4 w-4" /> Add repo
                </Button>
            }
        >
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}

            {repos.length === 0 ? (
                <EmptyState
                    className={FORM}
                    title="No repositories indexed"
                    action={
                        <Button onClick={() => setDraftRepo({})}>
                            <Plus className="h-4 w-4" /> Add repository
                        </Button>
                    }
                >
                    Add a local repository path to enable code context.
                </EmptyState>
            ) : (
                <div className={`${FORM} grid grid-cols-1 gap-3`}>
                    {repos.map(repo => {
                        const isIndexing = repo.status === 'indexing' || reindexingIds.has(repo.id);
                        const isStopping = repo.status === 'stopping' || stoppingIds.has(repo.id);
                        const deleteBlocked = isIndexing && !isStopping;
                        return (
                            <div
                                key={repo.id}
                                className="group cursor-pointer rounded-lg border border-border bg-surface p-4 transition-colors hover:border-border-strong"
                                onClick={() => setDraftRepo(repo)}
                            >
                                <div className="flex items-start justify-between gap-3">
                                    <div className="min-w-0">
                                        <h4 className="flex items-center gap-2 text-sm font-semibold text-text">
                                            {/* The card opens on a click; its name does the same for a keyboard (F236). */}
                                            <button
                                                type="button"
                                                onClick={e => { e.stopPropagation(); setDraftRepo(repo); }}
                                                className="text-left rounded-sm"
                                            >
                                                {repo.name}
                                            </button>
                                            <Badge tone={getStatusTone(repo.status)} size="sm">
                                                <span className="inline-flex items-center gap-1">
                                                    {(isIndexing || isStopping) && <Spinner size={10} />}
                                                    {sentence(!embedCode && repo.status === 'pending' ? 'not indexed' : repo.status)}
                                                </span>
                                            </Badge>
                                        </h4>
                                        <p className="mt-1 max-w-lg truncate font-code text-xs text-text-faint">{repo.path}</p>
                                    </div>

                                    {/* Action buttons — visible on hover, and on focus for a keyboard */}
                                    <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 transition-opacity">
                                        {embedCode && (isIndexing || isStopping ? (
                                            /* Stop / Force-reset button */
                                            <IconButton
                                                size="sm"
                                                icon={StopIcon}
                                                onClick={e => handleStop(repo.id, e)}
                                                label={`${isStopping ? 'Force reset' : 'Stop indexing'} ${repo.name}`}
                                                title={isStopping ? 'Force reset stuck status' : 'Stop indexing'}
                                                className={isStopping ? 'text-warning hover:text-warning' : 'text-danger hover:text-danger'}
                                            />
                                        ) : (
                                            /* Re-index button — when idle */
                                            <IconButton
                                                size="sm"
                                                icon={RefreshCw}
                                                onClick={e => handleReindex(repo.id, e)}
                                                label={`Re-index ${repo.name}`}
                                                title="Re-index"
                                            />
                                        ))}
                                        <IconButton
                                            size="sm"
                                            icon={Trash2}
                                            onClick={e => { e.stopPropagation(); setConfirmDeleteId(repo.id); }}
                                            label={`Delete ${repo.name}`}
                                            disabled={deleteBlocked}
                                            title={deleteBlocked ? 'Cannot delete while indexing' : 'Delete'}
                                            // Disabled, it still shows why on hover and a click stops here, not on the card, as before the kit.
                                            className="hover:text-danger disabled:pointer-events-auto disabled:cursor-not-allowed"
                                        />
                                    </div>
                                </div>

                                {repo.description && (
                                    <p className="mt-3 line-clamp-2 text-sm text-text-muted">{repo.description}</p>
                                )}

                                {repo.status === 'error' && repo.error_message && (
                                    <ErrorNote className="mt-3">
                                        <span className="block break-words whitespace-pre-wrap font-code text-xs">
                                            {repo.error_message}
                                        </span>
                                    </ErrorNote>
                                )}

                                {embedCode && (
                                    <div className="mt-3 flex gap-1.5 text-xs text-text-faint">
                                        <span>
                                            {(repo.file_count || 0).toLocaleString()} chunks
                                            {isIndexing || isStopping ? ' indexed so far…' : ' indexed'}
                                        </span>
                                        {repo.last_indexed && !isIndexing && !isStopping && (
                                            <span>· Updated {new Date(repo.last_indexed).toLocaleString()}</span>
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            )}

            <ConfirmationModal
                isOpen={!!confirmDeleteId}
                title="Delete repository"
                message="Are you sure you want to delete this repository and its index? This action cannot be undone."
                onConfirm={() => {
                    if (confirmDeleteId) handleDeleteRepo(confirmDeleteId);
                    setConfirmDeleteId(null);
                }}
                onClose={() => setConfirmDeleteId(null)}
            />
        </Section>
    );
}
