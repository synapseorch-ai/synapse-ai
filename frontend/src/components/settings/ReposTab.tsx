import React, { useState, useEffect, useCallback } from 'react';
import { Plus, Trash2, FolderGit2, RefreshCw, Loader2, ChevronDown, ChevronUp, Info, Square } from 'lucide-react';
import { ConfirmationModal } from './ConfirmationModal';
import { ToastNotification } from './ToastNotification';

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

    const getStatusColor = (status: string) => {
        switch (status) {
            case 'indexed':  return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
            case 'indexing': return 'bg-blue-500/10 text-blue-400 border-blue-500/20';
            case 'stopping': return 'bg-orange-500/10 text-orange-400 border-orange-500/20';
            case 'stopped':  return 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20';
            case 'error':    return 'bg-red-500/10 text-red-400 border-red-500/20';
            default:         return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
        }
    };

    if (isLoading) return <div className="p-8 text-center text-zinc-500">Loading…</div>;

    /* ── Form view ─────────────────────────────────────────────────────── */
    if (draftRepo !== null) {
        return (
            <div className="space-y-8">
                {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
                {embedCode && !embeddingModel && (
                    <div className="flex items-start gap-3 p-3 bg-amber-500/5 border border-amber-500/20 text-xs text-amber-400">
                        <Info className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                        <span>
                            No embedding model configured. Indexing requires an embedding model.{' '}
                            <a href="/settings/models" className="underline hover:text-amber-300 transition-colors">
                                Go to Models settings →
                            </a>
                        </span>
                    </div>
                )}
                <div className="mb-4">
                    <h3 className="text-lg font-bold text-white flex items-center gap-2">
                        <FolderGit2 className="h-5 w-5" />
                        {draftRepo.id ? 'Edit Repo' : 'Add New Repo'}
                    </h3>
                    <p className="text-zinc-500 text-sm mt-1">Configure codebase indexing settings.</p>
                </div>

                <div className="space-y-4">
                    <div className="space-y-2">
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Repo Name</label>
                        <input
                            type="text"
                            value={draftRepo.name || ''}
                            onChange={e => setDraftRepo({ ...draftRepo, name: e.target.value })}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono"
                            placeholder="e.g. Frontend App"
                            autoComplete="off"
                        />
                    </div>
                    <div className="space-y-2">
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Absolute Directory Path</label>
                        <input
                            type="text"
                            value={draftRepo.path || ''}
                            onChange={e => setDraftRepo({ ...draftRepo, path: e.target.value })}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono"
                            placeholder="/home/user/projects/app"
                            autoComplete="off"
                        />
                    </div>
                    <div className="space-y-2">
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Interconnection Description</label>
                        <textarea
                            value={draftRepo.description || ''}
                            onChange={e => setDraftRepo({ ...draftRepo, description: e.target.value })}
                            className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono min-h-[80px]"
                            placeholder="Help the LLM understand what this repo contains…"
                        />
                    </div>

                    {/* ── Advanced Settings Toggle ─────────────────────── */}
                    <div>
                        <button
                            type="button"
                            onClick={() => setShowAdvanced(p => !p)}
                            className="flex items-center gap-1.5 text-xs text-zinc-500 hover:text-zinc-300 transition-colors group"
                        >
                            {showAdvanced ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                            <span className="underline underline-offset-2 decoration-dotted group-hover:decoration-solid transition-all">
                                Advanced settings
                            </span>
                        </button>
                    </div>

                    {showAdvanced && (
                        <div className="space-y-4 pt-1 border-t border-zinc-800/60">
                            {/* ── Excluded Patterns ───────────────────────────────── */}
                            <div className="space-y-2">
                                <div className="flex items-center gap-2">
                                    <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Extra Excluded Patterns</label>
                                    <span className="text-[10px] px-1.5 py-0.5 bg-blue-500/10 text-blue-400 border border-blue-500/20 font-mono">one per line</span>
                                </div>

                                {/* Pattern-matching explainer */}
                                <div className="flex gap-2 p-3 bg-amber-500/5 border border-amber-500/15 text-xs text-amber-300">
                                    <Info className="w-3.5 h-3.5 mt-0.5 shrink-0" />
                                    <div className="space-y-1">
                                        <p><span className="font-mono text-white">data</span> → excludes <em>every</em> directory named <span className="font-mono">data</span> (backend/data AND frontend/data)</p>
                                        <p><span className="font-mono text-white">backend/data</span> → excludes <em>only</em> <span className="font-mono">backend/data</span>, leaving <span className="font-mono">frontend/data</span> indexed</p>
                                        <p><span className="font-mono text-white">*.json</span> → excludes all JSON files anywhere in the tree</p>
                                    </div>
                                </div>

                                <textarea
                                    value={excludedText}
                                    onChange={e => setExcludedText(e.target.value)}
                                    className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono min-h-[80px]"
                                    placeholder={`.*\nbackend/data\nfixtures`}
                                />

                                {/* Collapsible base exclusions */}
                                <button
                                    type="button"
                                    onClick={() => setShowBasePatterns(p => !p)}
                                    className="flex items-center gap-1.5 text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
                                >
                                    {showBasePatterns ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                                    Always excluded automatically ({BASE_EXCLUDED.length} base patterns)
                                </button>
                                {showBasePatterns && (
                                    <div className="flex flex-wrap gap-1 p-3 bg-zinc-950 border border-zinc-800">
                                        {BASE_EXCLUDED.map(p => (
                                            <span key={p} className="text-[10px] px-1.5 py-0.5 bg-zinc-800 text-zinc-400 font-mono">{p}</span>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {/* ── Included Patterns ───────────────────────────────── */}
                            <div className="space-y-2">
                                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Included File Types</label>
                                <textarea
                                    value={includedText}
                                    onChange={e => setIncludedText(e.target.value)}
                                    className="w-full bg-zinc-900 border border-zinc-800 p-3 text-sm text-white focus:border-white focus:outline-none transition-colors font-mono min-h-[60px]"
                                    placeholder={`*.py\n*.ts\n*.tsx\n*.md`}
                                />
                                <p className="text-xs text-zinc-600">Only files matching these globs will be indexed. Leave blank to use defaults.</p>
                            </div>
                        </div>
                    )}

                    <div className="flex gap-4 justify-end pt-4">
                        <button
                            onClick={() => setDraftRepo(null)}
                            className="px-6 py-2.5 text-sm font-bold text-zinc-400 hover:text-white transition-all"
                        >
                            Cancel
                        </button>
                        <button
                            onClick={handleSaveRepo}
                            disabled={isSaving}
                            className="flex items-center gap-2 px-6 py-2.5 text-sm font-bold bg-white text-black hover:bg-zinc-200 transition-all shadow-lg disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {isSaving && <Loader2 className="w-4 h-4 animate-spin" />}
                            {isSaving ? 'Saving…' : 'Save Repository'}
                        </button>
                    </div>
                </div>
            </div>
        );
    }

    /* ── List view ─────────────────────────────────────────────────────── */
    return (
        <div className="space-y-8">
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
            <div className="mb-4 flex items-center justify-between">
                <div>
                    <h3 className="text-lg font-bold text-white flex items-center gap-2">
                        <FolderGit2 className="h-5 w-5" />
                        Code Repositories
                    </h3>
                    <p className="text-zinc-500 text-sm mt-1">
                        Manage your agent's codebases for semantic code searching.
                    </p>
                </div>
                <button
                    onClick={() => setDraftRepo({})}
                    className="flex items-center gap-2 px-4 py-2 text-sm font-bold bg-white text-black hover:bg-zinc-200 transition-all shadow-lg"
                >
                    <Plus className="w-4 h-4" /> Add Repo
                </button>
            </div>

            {repos.length === 0 ? (
                <div className="text-center py-12 border border-dashed border-zinc-800 bg-zinc-900/50">
                    <FolderGit2 className="w-8 h-8 mx-auto text-zinc-600 mb-3" />
                    <h3 className="text-sm font-bold text-zinc-100">No repositories indexed</h3>
                    <p className="text-sm text-zinc-500 mt-1 mb-6">
                        Add a local repository path to enable code context.
                    </p>
                    <button
                        onClick={() => setDraftRepo({})}
                        className="inline-flex items-center gap-2 px-6 py-2.5 text-sm font-bold bg-white text-black hover:bg-zinc-200 transition-all shadow-lg"
                    >
                        <Plus className="w-4 h-4" /> Add Repository
                    </button>
                </div>
            ) : (
                <div className="grid grid-cols-1 gap-4">
                    {repos.map(repo => {
                        const isIndexing = repo.status === 'indexing' || reindexingIds.has(repo.id);
                        const isStopping = repo.status === 'stopping' || stoppingIds.has(repo.id);
                        return (
                            <div
                                key={repo.id}
                                className="p-4 border border-zinc-800 bg-zinc-900/50 hover:border-zinc-600 transition-colors cursor-pointer group"
                                onClick={() => setDraftRepo(repo)}
                            >
                                <div className="flex justify-between items-start mb-2">
                                    <div>
                                        <h4 className="font-bold text-white text-lg flex items-center gap-2">
                                            {repo.name}
                                            <span className={`text-[10px] font-bold px-2 py-0.5 border uppercase tracking-wide inline-flex items-center gap-1 ${getStatusColor(repo.status)}`}>
                                                {(isIndexing || isStopping) && <Loader2 className="w-2.5 h-2.5 animate-spin" />}
                                                {!embedCode && repo.status === 'pending' ? 'not indexed' : repo.status}
                                            </span>
                                        </h4>
                                        <p className="text-xs text-zinc-500 font-mono truncate max-w-lg mt-1">{repo.path}</p>
                                    </div>

                                    {/* Action buttons — only visible on hover */}
                                    <div className="flex items-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity">
                                        {embedCode && (isIndexing || isStopping ? (
                                            /* Stop / Force-reset button */
                                            <button
                                                onClick={e => handleStop(repo.id, e)}
                                                title={isStopping ? 'Force reset stuck status' : 'Stop Indexing'}
                                                className={`p-2 transition-colors rounded ${
                                                    isStopping
                                                        ? 'text-orange-400 hover:text-orange-300 hover:bg-orange-500/10'
                                                        : 'text-red-400 hover:text-red-300 hover:bg-red-500/10'
                                                }`}
                                            >
                                                <Square className="w-4 h-4 fill-current" />
                                            </button>
                                        ) : (
                                            /* Re-index button — when idle */
                                            <button
                                                onClick={e => handleReindex(repo.id, e)}
                                                title="Re-Index"
                                                className="p-2 text-zinc-400 hover:text-white hover:bg-zinc-800 transition-colors rounded"
                                            >
                                                <RefreshCw className="w-4 h-4" />
                                            </button>
                                        ))}
                                        <button
                                            onClick={e => { e.stopPropagation(); setConfirmDeleteId(repo.id); }}
                                            disabled={isIndexing && !isStopping}
                                            title={isIndexing && !isStopping ? 'Cannot delete while indexing' : 'Delete'}
                                            className={`p-2 transition-colors rounded ${
                                                isIndexing && !isStopping
                                                    ? 'text-zinc-700 cursor-not-allowed'
                                                    : 'text-zinc-400 hover:text-red-500 hover:bg-red-500/10'
                                            }`}
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                </div>

                                {repo.description && (
                                    <p className="text-sm text-zinc-400 mt-3 line-clamp-2">{repo.description}</p>
                                )}

                                {repo.status === 'error' && repo.error_message && (
                                    <div className="mt-3 p-3 bg-red-500/5 border border-red-500/20 rounded">
                                        <p className="text-xs text-red-400 font-mono break-words whitespace-pre-wrap">
                                            {repo.error_message}
                                        </p>
                                    </div>
                                )}

                                {embedCode && (
                                    <div className="flex gap-4 mt-4 text-xs font-bold text-zinc-600 uppercase tracking-wider">
                                        <span>
                                            {(repo.file_count || 0).toLocaleString()} CHUNKS
                                            {isIndexing || isStopping ? ' INDEXED SO FAR…' : ' INDEXED'}
                                        </span>
                                        {repo.last_indexed && !isIndexing && !isStopping && (
                                            <span>• UPDATED {new Date(repo.last_indexed).toLocaleString()}</span>
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
                title="Delete Repository"
                message="Are you sure you want to delete this repository and its index? This action cannot be undone."
                onConfirm={() => {
                    if (confirmDeleteId) handleDeleteRepo(confirmDeleteId);
                    setConfirmDeleteId(null);
                }}
                onClose={() => setConfirmDeleteId(null)}
            />
        </div>
    );
}
