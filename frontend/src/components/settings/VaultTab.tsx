/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';
import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { FolderOpen, Folder, FileText, FileJson, FileCode, Image as ImageIcon, ChevronRight, ChevronDown, Plus, Trash2, RefreshCw, FolderPlus, FilePlus, AlignLeft, Cloud, Upload, Library, Sheet, Download, Maximize2, Minimize2 } from 'lucide-react';
import { DetailHead, LIST_DETAIL, ROW_CURRENT, showDetail } from '@/components/run';
import {
    Badge, Button, buttonStyles, CodeEditor, ConfirmDialog, EmptyState, Field, IconButton, Input, MarkdownEditor, Modal,
    SearchInput, Segmented, Spinner, Toast, useToast,
} from '@/components/ui';
import { cn } from '@/lib/cn';
import { DocBadge, useVaultKnowledge, vaultKnowledge, type VaultKnowledge } from '@/components/knowledge/VaultKnowledge';
import { ACCEPTED, REASONS, uploadToKnowledge, useKnowledgeEnabled, type KnowledgeBase } from '@/lib/knowledge';
import {
    codeLanguage, DownloadOnly, fileKind, formatSize, formatWhen, ImageView, rawUrl, TableView,
} from './VaultViewer';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface VaultNode {
    name: string;
    path: string;
    type: 'file' | 'folder';
    ext?: string;
    children?: VaultNode[];
    /** Files (F1207): bytes, ISO time, and whether runs write it (the engine's folders), so it opens read-only. */
    size?: number | null;
    modified?: string | null;
    read_only?: boolean;
}

// ---------------------------------------------------------------------------
// Tree Node Component
// ---------------------------------------------------------------------------

function TreeNode({
    node,
    selectedPath,
    onSelect,
    onDelete,
    onCreateInFolder,
    knowledge,
    openPath,
    depth = 0,
}: {
    node: VaultNode;
    selectedPath: string | null;
    onSelect: (node: VaultNode) => void;
    onDelete: (node: VaultNode) => void;
    onCreateInFolder: (folderPath: string) => void;
    knowledge?: VaultKnowledge;
    /** The open file: only the folders that hold it start open (Naveen, M2h-2: the tree opened everything). */
    openPath?: string | null;
    depth?: number;
}) {
    const [expanded, setExpanded] = useState(() => !!openPath && openPath.startsWith(`${node.path}/`));

    const isSelected = selectedPath === node.path;
    const isFolder = node.type === 'folder';

    const handleClick = () => {
        if (isFolder) {
            setExpanded(e => !e);
        } else {
            onSelect(node);
        }
    };

    return (
        <div>
            {/* The name is a button, so a keyboard opens a file or a folder as a
                click does (F219); the actions beside it are its siblings, shown
                whenever the row has focus as well as on hover. */}
            <div
                className={cn(
                    'group flex min-h-9 items-center gap-1 rounded-md pr-1.5 text-sm transition-colors',
                    isSelected ? ROW_CURRENT : 'text-text-muted hover:bg-row-hover hover:text-text',
                )}
            >
                <button
                    type="button"
                    onClick={handleClick}
                    aria-expanded={isFolder ? expanded : undefined}
                    aria-current={isSelected ? 'true' : undefined}
                    className="flex min-h-9 min-w-0 flex-1 items-center gap-1.5 rounded-md text-left"
                    style={{ paddingLeft: `${8 + depth * 16}px` }}
                    title={isFolder ? undefined : [formatSize(node.size), formatWhen(node.modified)].filter(Boolean).join(' · ') || undefined}
                >
                    {/* Expand/collapse arrow for folders */}
                    {isFolder ? (
                        <span className="shrink-0 text-text-faint">
                            {expanded ? <ChevronDown className="size-3.5" aria-hidden /> : <ChevronRight className="size-3.5" aria-hidden />}
                        </span>
                    ) : (
                        <span className="w-3.5 shrink-0" />
                    )}
                    <span className="shrink-0 text-text-faint"><FileIcon node={node} open={expanded} /></span>
                    <span className="flex-1 truncate">{node.name}</span>
                    {!isFolder && formatSize(node.size) && (
                        <span className="shrink-0 font-code text-[11px] tabular-nums text-text-faint md:group-focus-within:hidden md:group-hover:hidden">
                            {formatSize(node.size)}
                        </span>
                    )}
                </button>

                {isFolder && knowledge?.baseAt(node.path) && (
                    <span className="shrink-0" title={`Knowledge base: ${knowledge.baseAt(node.path)!.name}`}>
                        <Badge size="sm" tone="accent">Knowledge</Badge>
                    </span>
                )}
                {!isFolder && knowledge?.docAt(node.path) && <DocBadge doc={knowledge.docAt(node.path)!} />}

                {/* The row's actions: quiet until the row is hovered or focused (always there on a phone). */}
                <span className="flex shrink-0 items-center md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100">
                    {isFolder && knowledge?.baseHolding(node.path) && !knowledge.managed(node.path) && (
                        <IconButton
                            size="sm"
                            icon={Upload}
                            label={`Upload files to ${node.name}`}
                            onClick={() => knowledge.onUpload(node.path, knowledge.baseHolding(node.path)!)}
                        />
                    )}
                    {isFolder && !knowledge?.managed(node.path) && (
                        <IconButton size="sm" icon={FilePlus} label={`New file in ${node.name}`} onClick={() => onCreateInFolder(node.path)} />
                    )}
                    <IconButton size="sm" icon={Trash2} tone="danger" label={`Delete ${node.name}`} onClick={() => onDelete(node)} />
                </span>
            </div>

            {/* Children */}
            {isFolder && expanded && node.children && node.children.length > 0 && (
                <div>
                    {node.children.map(child => (
                        <TreeNode
                            key={child.path}
                            node={child}
                            selectedPath={selectedPath}
                            onSelect={onSelect}
                            onDelete={onDelete}
                            onCreateInFolder={onCreateInFolder}
                            knowledge={knowledge}
                            openPath={openPath}
                            depth={depth + 1}
                        />
                    ))}
                </div>
            )}
            {isFolder && expanded && (!node.children || node.children.length === 0) && (
                <div
                    className="py-1 text-xs italic text-text-faint"
                    style={{ paddingLeft: `${8 + (depth + 2) * 16}px` }}
                >
                    Empty folder
                </div>
            )}
        </div>
    );
}

/** A file's kind, by its extension: one quiet glyph (colour is for meaning only). */
function FileIcon({ node, open, className = 'size-3.5' }: { node: VaultNode; open?: boolean; className?: string }) {
    if (node.type === 'folder') return open ? <FolderOpen className={className} aria-hidden /> : <Folder className={className} aria-hidden />;
    if (node.ext === '.json') return <FileJson className={className} aria-hidden />;
    if (node.ext === '.txt') return <AlignLeft className={className} aria-hidden />;
    const kind = fileKind(node.ext);
    if (kind === 'table') return <Sheet className={className} aria-hidden />;
    if (kind === 'image') return <ImageIcon className={className} aria-hidden />;
    if (kind === 'code') return <FileCode className={className} aria-hidden />;
    return <FileText className={className} aria-hidden />;
}

const KIND_WORD: Record<string, string> = {
    '.json': 'JSON', '.txt': 'Plain text', '.csv': 'CSV', '.tsv': 'TSV', '.pdf': 'PDF', '.py': 'Python',
};

/** What a file is, in a word: its kind's, or its extension's ("YAML file"), or Markdown's. */
function kindWord(node: VaultNode): string {
    if (KIND_WORD[node.ext ?? '']) return KIND_WORD[node.ext ?? ''];
    const kind = fileKind(node.ext);
    if (kind === 'markdown') return 'Markdown';
    if (kind === 'image') return 'Image';
    return node.ext ? `${node.ext.slice(1).toUpperCase()} file` : 'Text file';
}

type SortBy = 'name' | 'modified' | 'size';

/** The tree in the order picked: folders first, then files by name, newest first or largest first (F1207). */
function sortTree(nodes: VaultNode[], by: SortBy): VaultNode[] {
    const files = nodes.filter(n => n.type === 'file');
    const folders = nodes.filter(n => n.type === 'folder').map(n => ({ ...n, children: sortTree(n.children ?? [], by) }));
    const byName = (a: VaultNode, b: VaultNode) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    const order = by === 'modified'
        ? (a: VaultNode, b: VaultNode) => (b.modified ?? '').localeCompare(a.modified ?? '') || byName(a, b)
        : by === 'size' ? (a: VaultNode, b: VaultNode) => (b.size ?? -1) - (a.size ?? -1) || byName(a, b) : byName;
    return [...folders.sort(byName), ...files.sort(order)];
}

/** Every file whose name or path holds `query`, in the order picked. */
function matchingFiles(nodes: VaultNode[], query: string, by: SortBy): VaultNode[] {
    const q = query.trim().toLowerCase();
    const out: VaultNode[] = [];
    const walk = (list: VaultNode[]) => list.forEach(n => n.type === 'folder' ? walk(n.children ?? []) : n.path.toLowerCase().includes(q) && out.push(n));
    walk(nodes);
    return sortTree(out, by);
}

/** The first file in the tree, depth first: what opens when nothing is picked (UX1: never an empty pane). */
function firstFile(nodes: VaultNode[]): VaultNode | null {
    for (const node of nodes) {
        if (node.type === 'file') return node;
    }
    for (const node of nodes) {
        const inside = node.children ? firstFile(node.children) : null;
        if (inside) return inside;
    }
    return null;
}

// ---------------------------------------------------------------------------
// Create Dialog
// ---------------------------------------------------------------------------

function CreateDialog({
    type,
    parentPath,
    onClose,
    onCreate,
}: {
    type: 'file' | 'folder';
    parentPath: string;
    onClose: () => void;
    onCreate: (name: string, ext?: string) => void;
}) {
    const [name, setName] = useState('');
    const [fileType, setFileType] = useState<'md' | 'json' | 'txt' | 'csv'>('md');

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!name.trim()) return;
        if (type === 'file') {
            const finalName = name.trim().endsWith(`.${fileType}`) ? name.trim() : `${name.trim()}.${fileType}`;
            onCreate(finalName, `.${fileType}`);
        } else {
            onCreate(name.trim());
        }
    };

    return (
        <Modal
            open
            onClose={onClose}
            size="sm"
            title={type === 'file' ? 'New file' : 'New folder'}
            footer={
                <>
                    <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
                    <Button type="submit" form="vault-create" disabled={!name.trim()}>Create</Button>
                </>
            }
        >
            <form id="vault-create" onSubmit={handleSubmit} className="grid gap-4">
                {parentPath && (
                    <p className="m-0 text-xs text-text-faint">
                        Location: <span className="font-code text-text-muted">/{parentPath}</span>
                    </p>
                )}

                {type === 'file' && (
                    <Segmented
                        label="File type"
                        value={fileType}
                        onChange={setFileType}
                        options={[
                            { value: 'md', label: 'Markdown', icon: <FileText className="size-3.5" aria-hidden /> },
                            { value: 'json', label: 'JSON', icon: <FileJson className="size-3.5" aria-hidden /> },
                            { value: 'txt', label: 'Text', icon: <AlignLeft className="size-3.5" aria-hidden /> },
                            { value: 'csv', label: 'CSV', icon: <Sheet className="size-3.5" aria-hidden /> },
                        ]}
                    />
                )}

                <Field
                    label={type === 'file' ? 'File name' : 'Folder name'}
                    hint={type === 'file' ? <>Extension <span className="font-code">.{fileType}</span> will be appended automatically if not included.</> : undefined}
                >
                    <Input
                        id="vault-type-file-file-name-folder-n"
                        autoFocus
                        type="text"
                        value={name}
                        onChange={e => setName(e.target.value)}
                        placeholder={type === 'file' ? `my-document.${fileType}` : 'my-folder'}
                        className="font-code"
                    />
                </Field>
            </form>
        </Modal>
    );
}

// ---------------------------------------------------------------------------
// Delete Confirmation
// ---------------------------------------------------------------------------

function DeleteConfirm({
    node,
    onConfirm,
    onCancel,
}: {
    node: VaultNode;
    onConfirm: () => void;
    onCancel: () => void;
}) {
    return (
        <ConfirmDialog
            open
            onClose={onCancel}
            onConfirm={onConfirm}
            title={node.type === 'folder' ? 'Delete folder' : 'Delete file'}
            confirmLabel="Delete"
            description={
                node.type === 'folder'
                    ? <>Delete folder <span className="font-code text-danger">{node.name}</span> and everything in it? This cannot be undone.</>
                    : <>Delete <span className="font-code text-danger">{node.name}</span>? This cannot be undone.</>
            }
        />
    );
}

// ---------------------------------------------------------------------------
// Editors: one height, taking the rest of the viewport under the head
// ---------------------------------------------------------------------------

const EDITOR_HEIGHT = 'h-[clamp(280px,calc(100vh-330px),900px)] resize-none';
/** The code editor's frame takes its height from a style; these win over it (F1207). Full screen: the window. */
const CODE_HEIGHT = '!h-[clamp(280px,calc(100vh-330px),900px)]';
const CODE_HEIGHT_FULL = '!h-[calc(100vh-9rem)]';

// ---------------------------------------------------------------------------
// Main VaultTab Component
// ---------------------------------------------------------------------------

export function VaultTab({ initialPath }: { initialPath?: string } = {}) {
    const [tree, setTree] = useState<VaultNode[]>([]);
    const [loading, setLoading] = useState(true);
    const [selectedNode, setSelectedNode] = useState<VaultNode | null>(null);
    const [fileContent, setFileContent] = useState('');
    const [savedContent, setSavedContent] = useState('');
    const [loadingFile, setLoadingFile] = useState(false);
    const [savingFile, setSavingFile] = useState(false);

    // Dialogs
    const [createDialog, setCreateDialog] = useState<{ type: 'file' | 'folder'; parentPath: string } | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<VaultNode | null>(null);

    // Context for right-click / folder selection for new items
    const [contextFolder, setContextFolder] = useState<string>('');

    const [s3Bucket, setS3Bucket] = useState<string>('');
    const [storageSource, setStorageSource] = useState<'s3' | 'local'>('s3');

    // Knowledge (D157): only while it is on, and only on the Vault itself — a
    // bucket's local working copy is not what a knowledge base reads.
    const knowledgeOn = useKnowledgeEnabled() === true && (!s3Bucket || storageSource === 's3');
    const [treeLoads, setTreeLoads] = useState(0);
    const kn = useVaultKnowledge(knowledgeOn, treeLoads);
    const [binary, setBinary] = useState(false);
    // F1207: a file over the editor's 5 MB (the API's words), one runs write, and the pane over the whole window.
    const [tooLarge, setTooLarge] = useState<string | null>(null);
    const [readOnly, setReadOnly] = useState(false);
    const [fullScreen, setFullScreen] = useState(false);
    const [filter, setFilter] = useState('');
    const [sortBy, setSortBy] = useState<SortBy>('name');
    const uploadInput = React.useRef<HTMLInputElement>(null);
    const [uploadTarget, setUploadTarget] = useState<{ folder: string; kb: KnowledgeBase } | null>(null);
    const [uploading, setUploading] = useState(0);
    const { toast, show, dismiss } = useToast();

    const fetchTree = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch(`/api/vault/tree?source=${storageSource}`);
            if (res.ok) {
                const data = await res.json();
                setTree(data.tree || []);
                setTreeLoads(n => n + 1);
            }
        } catch (e) {
            console.error('Failed to load vault tree', e);
        } finally {
            setLoading(false);
        }
    }, [storageSource]);

    const knowledge: VaultKnowledge | undefined = vaultKnowledge(kn, (folder, kb) => {
        setUploadTarget({ folder, kb });
        uploadInput.current?.click();
    });

    const uploadFiles = async (files: FileList) => {
        if (!uploadTarget) return;
        const { folder, kb } = uploadTarget;
        const list = [...files];
        setUploading(n => n + list.length);
        let failed = 0;
        for (const file of list) {
            try {
                await uploadToKnowledge(kb.id, file, { folder });
            } catch (e) {
                failed += 1;
                show(`${file.name}: ${e instanceof Error ? e.message : String(e)}`, 'danger');
            } finally {
                setUploading(n => n - 1);
            }
        }
        if (failed < list.length) show(`${list.length - failed} file(s) added to ${kb.name}.`);
        await fetchTree();
        kn.reload();
    };

    useEffect(() => { fetchTree(); }, [fetchTree]);

    // Opened from an answer's Sources (`/vault?path=…`, B1b): that file, once the
    // tree is in. Once only; a later visit to the tree is the person's.
    const [linkedPath, setLinkedPath] = useState<string | null>(initialPath ?? null);
    useEffect(() => {
        if (!linkedPath || tree.length === 0) return;
        const find = (nodes: VaultNode[]): VaultNode | null => {
            for (const node of nodes) {
                if (node.type === 'file' && node.path === linkedPath) return node;
                const inside = node.children ? find(node.children) : null;
                if (inside) return inside;
            }
            return null;
        };
        const node = find(tree);
        if (node) setSelectedNode(node);
        setLinkedPath(null);
    }, [linkedPath, tree]);

    // UX1: the pane never opens empty. Nothing picked (or the picked file gone)
    // opens the first file in the tree; a `?path=` link above still wins.
    const openNode: VaultNode | null = selectedNode?.type === 'file' ? selectedNode : (linkedPath ? null : firstFile(tree));
    const detailRef = React.useRef<HTMLElement>(null);

    // Fetch scale config to show S3 banner when configured
    useEffect(() => {
        fetch('/api/scale/config')
            .then(r => r.ok ? r.json() : null)
            .then(data => { if (data?.s3_bucket) setS3Bucket(data.s3_bucket); })
            .catch(() => {});
    }, []);

    // Load file content when a file is opened
    const openPath = openNode?.path ?? null;
    // A click on a file reads it again, even the open one while it has no
    // unsaved edits (its text may have changed under it, and a keyboard's Enter
    // on it should do something).
    const [reads, setReads] = useState(0);
    const openKind = fileKind(openNode?.ext);
    const shownAsIs = openKind === 'image' || openKind === 'pdf';  // never read as text: `/api/vault/raw` has them
    useEffect(() => {
        if (!openPath) return;
        setBinary(false);
        setTooLarge(null);
        setReadOnly(false);
        if (shownAsIs) {
            setFileContent('');
            setSavedContent('');
            return;
        }
        setLoadingFile(true);
        fetch(`/api/vault/file?path=${encodeURIComponent(openPath)}&source=${storageSource}`)
            .then(async r => {
                // 415: bytes, nothing an editor can open (F323); 413: more than the editor opens (F1207).
                if (r.status === 415) setBinary(true);
                if (r.status === 413) setTooLarge((await r.json().catch(() => null))?.detail ?? 'Too large to open here.');
                return r.ok ? r.json() : null;
            })
            .then(data => {
                const c = data?.content ?? '';
                setFileContent(c);
                setSavedContent(c);
                setReadOnly(!!data?.read_only);
            })
            .catch(() => {
                setFileContent('');
                setSavedContent('');
            })
            .finally(() => setLoadingFile(false));
    }, [openPath, storageSource, reads, shownAsIs]);

    const editable = !!openNode && !shownAsIs && !binary && !tooLarge && !readOnly;
    const handleSave = async () => {
        if (!openNode || !editable) return;
        setSavingFile(true);
        try {
            const res = await fetch('/api/vault/file', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: openNode.path, content: fileContent, source: storageSource }),
            });
            if (res.ok) {
                setSavedContent(fileContent);
                show('Saved');
                fetchTree();  // its size and time
            } else {
                const detail = (await res.json().catch(() => null))?.detail;
                show(typeof detail === 'string' ? detail : `Could not save ${openNode.name}`, 'danger');
            }
        } catch {
            show(`Could not save ${openNode.name}`, 'danger');
        } finally {
            setSavingFile(false);
        }
    };

    const handleCreate = async (name: string) => {
        if (!createDialog) return;
        const endpoint = createDialog.type === 'file' ? '/api/vault/file' : '/api/vault/folder';
        try {
            const res = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: contextFolder, name, source: storageSource }),
            });
            if (res.ok) {
                const data = await res.json();
                setCreateDialog(null);
                await fetchTree();
                // Auto-select newly created file
                if (createDialog.type === 'file') {
                    setSelectedNode({ name: data.name, path: data.path, type: 'file', ext: data.ext });
                }
            } else {
                const detail = (await res.json().catch(() => null))?.detail;
                show(typeof detail === 'string' ? detail : `Could not create ${name}`, 'danger');
            }
        } catch (e) {
            console.error('Failed to create', e);
            show(`Could not create ${name}`, 'danger');
        }
    };

    const handleDelete = async () => {
        if (!deleteTarget) return;
        try {
            const res = await fetch('/api/vault/item', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: deleteTarget.path, source: storageSource }),
            });
            if (res.ok) {
                if (selectedNode?.path === deleteTarget.path ||
                    (deleteTarget.type === 'folder' && selectedNode?.path.startsWith(deleteTarget.path + '/'))) {
                    setSelectedNode(null);
                    setFileContent('');
                    setSavedContent('');
                }
                await fetchTree();
            } else {
                show(`Could not delete ${deleteTarget.name}`, 'danger');
            }
        } catch (e) {
            console.error('Failed to delete', e);
            show(`Could not delete ${deleteTarget.name}`, 'danger');
        } finally {
            setDeleteTarget(null);
        }
    };

    const isDirty = fileContent !== savedContent;

    // Ctrl/Cmd+S saves an edited file; Escape leaves full screen.
    const saveRef = React.useRef(handleSave);
    saveRef.current = handleSave;
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's' && isDirty && editable) {
                e.preventDefault();
                saveRef.current();
            } else if (e.key === 'Escape' && fullScreen) setFullScreen(false);
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [isDirty, editable, fullScreen]);

    const shownTree = React.useMemo(() => sortTree(tree, sortBy), [tree, sortBy]);
    const selectNode = (n: VaultNode) => {
        setSelectedNode(n);
        // Never over unsaved edits: the open file read again would lose them.
        if (!(n.path === openNode?.path && fileContent !== savedContent)) setReads(r => r + 1);
        // Set context folder for "new" actions
        if (n.type === 'folder') {
            setContextFolder(n.path);
        } else {
            const parts = n.path.split('/');
            setContextFolder(parts.slice(0, -1).join('/'));
        }
        showDetail(detailRef.current);
    };
    const found = React.useMemo(() => (filter.trim() ? matchingFiles(tree, filter, sortBy) : null), [tree, filter, sortBy]);
    const newAt = (type: 'file' | 'folder') => { setContextFolder(''); setCreateDialog({ type, parentPath: '' }); };
    const switchSource = (source: 's3' | 'local') => {
        setStorageSource(source); setSelectedNode(null); setFileContent(''); setSavedContent('');
    };
    const docHere = openNode ? knowledge?.docAt(openNode.path) : undefined;

    return (
        <div className="grid gap-5">
            {/* S3 storage, with the source switch */}
            {s3Bucket && (
                <div className="flex flex-wrap items-center gap-2 text-[13px] text-text-muted">
                    <Cloud className="size-4 shrink-0 text-text-faint" aria-hidden />
                    <span>
                        <span className="font-medium text-text">S3 connected</span>
                        {' — '}bucket: <span className="font-code text-text">{s3Bucket}</span>
                    </span>
                    <div role="group" aria-label="View" className="ml-auto flex items-center gap-1.5">
                        <span className="text-xs text-text-faint">View:</span>
                        {(['s3', 'local'] as const).map(source => (
                            <button
                                key={source}
                                type="button"
                                aria-pressed={storageSource === source}
                                onClick={() => switchSource(source)}
                                className={cn(
                                    'inline-flex h-7 items-center rounded-full border px-3 text-xs font-medium transition-colors',
                                    storageSource === source
                                        ? 'border-text bg-text text-bg'
                                        : 'border-border text-text-muted hover:border-border-strong hover:text-text',
                                )}
                            >
                                {source === 's3' ? 'S3' : 'Local'}
                            </button>
                        ))}
                    </div>
                </div>
            )}

            {!loading && tree.length === 0 ? (
                <EmptyState
                    title="Vault is empty"
                    action={
                        <div className="flex justify-center gap-2">
                            <Button variant="secondary" onClick={() => newAt('folder')}><FolderPlus className="size-4" aria-hidden /> New folder</Button>
                            <Button onClick={() => newAt('file')}><Plus className="size-4" aria-hidden /> New file</Button>
                        </div>
                    }
                >
                    Create <span className="font-code">.md</span>, <span className="font-code">.json</span>, <span className="font-code">.txt</span> or <span className="font-code">.csv</span> files
                    to use as context in agent prompts, and reference them with <span className="font-code">@[path]</span>.
                </EmptyState>
            ) : (
            <div className={LIST_DETAIL}>
            {/* The tree */}
            <nav aria-label="Files" className="grid min-w-0 gap-2 lg:sticky lg:top-4">
                <div className="flex items-center gap-1 border-b border-border pb-2">
                    <span className="flex-1 text-[13px] font-semibold text-text">Files</span>
                    <IconButton size="sm" icon={FolderPlus} label="New folder" title="New Folder" onClick={() => newAt('folder')} />
                    <IconButton size="sm" icon={FilePlus} label="New file" title="New File" onClick={() => newAt('file')} />
                    <IconButton size="sm" icon={RefreshCw} label="Refresh" onClick={fetchTree} />
                </div>

                {/* F1207: find a file by name or path, and the order files sit in. */}
                <div className="grid gap-2">
                    <SearchInput value={filter} onChange={setFilter} placeholder="Filter files…" aria-label="Filter files" className="max-w-none" />
                    <Segmented
                        label="Sort files by"
                        value={sortBy}
                        onChange={setSortBy}
                        options={[{ value: 'name', label: 'Name' }, { value: 'modified', label: 'Newest' }, { value: 'size', label: 'Largest' }]}
                    />
                </div>

                <div className="max-h-[calc(100vh-21rem)] overflow-y-auto modern-scrollbar max-lg:max-h-80">
                    {loading ? (
                        <div className="flex items-center gap-2 px-2 py-4 text-sm text-text-faint" role="status">
                            <Spinner size={14} /> Loading…
                        </div>
                    ) : found ? (
                        found.length === 0 ? (
                            <p className="m-0 px-2 py-4 text-sm text-text-faint">No file&apos;s name or path has “{filter.trim()}”.</p>
                        ) : (
                            <ul className="m-0 grid list-none gap-0.5 p-0" aria-label="Files found">
                                {found.map(n => (
                                    <li key={n.path}>
                                        <button
                                            type="button"
                                            onClick={() => selectNode(n)}
                                            aria-current={openNode?.path === n.path ? 'true' : undefined}
                                            className={cn(
                                                'grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2 rounded-md px-2 py-1.5 text-left text-sm',
                                                openNode?.path === n.path ? ROW_CURRENT : 'text-text-muted hover:bg-row-hover hover:text-text',
                                            )}
                                        >
                                            <span className="text-text-faint"><FileIcon node={n} /></span>
                                            <span className="min-w-0">
                                                <span className="block truncate">{n.name}</span>
                                                <span className="block truncate font-code text-[11px] text-text-faint">{n.path}</span>
                                            </span>
                                            <span className="font-code text-[11px] tabular-nums text-text-faint">{formatSize(n.size)}</span>
                                        </button>
                                    </li>
                                ))}
                            </ul>
                        )
                    ) : (
                        shownTree.map(node => (
                            <TreeNode
                                key={node.path}
                                node={node}
                                selectedPath={openNode?.path ?? null}
                                onSelect={selectNode}
                                onDelete={setDeleteTarget}
                                onCreateInFolder={(folderPath) => {
                                    setContextFolder(folderPath);
                                    setCreateDialog({ type: 'file', parentPath: folderPath });
                                }}
                                knowledge={knowledge}
                                openPath={openNode?.path ?? null}
                            />
                        ))
                    )}
                </div>

                <input
                    ref={uploadInput}
                    type="file"
                    multiple
                    accept={ACCEPTED.join(',')}
                    className="hidden"
                    aria-hidden
                    tabIndex={-1}
                    onChange={e => { if (e.target.files) uploadFiles(e.target.files); e.target.value = ''; }}
                />
                {uploading > 0 && (
                    <div className="flex items-center gap-2 border-t border-border pt-2 text-xs text-text-muted" role="status">
                        <Spinner size={12} /> Uploading {uploading} file{uploading === 1 ? '' : 's'}…
                    </div>
                )}

                <p className="m-0 border-t border-border pt-2 text-xs leading-relaxed text-text-faint">
                    Files here can be referenced in agent prompts using <span className="font-code">@[path]</span>
                </p>
            </nav>

            {/* The open file */}
            <section
                ref={detailRef}
                aria-label="File"
                className={cn(
                    'grid min-w-0 scroll-mt-4 content-start gap-4',
                    fullScreen && 'fixed inset-0 z-50 overflow-auto bg-bg p-4 md:p-6',
                )}
            >
                {openNode && (
                    <>
                        <DetailHead
                            leading={<span className="mt-1 text-text-faint"><FileIcon node={openNode} className="size-5" /></span>}
                            title={openNode.name}
                            sub={
                                <>
                                    <span>{kindWord(openNode)}</span>
                                    {formatSize(openNode.size) && <> · <span>{formatSize(openNode.size)}</span></>}
                                    {formatWhen(openNode.modified) && <> · <span>{formatWhen(openNode.modified)}</span></>}
                                    {' · '}<span className="font-code">{openNode.path}</span>
                                </>
                            }
                            actions={
                                <>
                                    {readOnly && (
                                        <span title="Runs write the files in this folder, so they open read-only.">
                                            <Badge size="sm">Read-only</Badge>
                                        </span>
                                    )}
                                    {isDirty && (
                                        <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs font-medium text-text-muted">Unsaved</span>
                                    )}
                                    {editable && (
                                        <Button onClick={handleSave} disabled={savingFile || !isDirty} loading={savingFile}>
                                            Save
                                        </Button>
                                    )}
                                    <a
                                        href={rawUrl(openNode.path, storageSource, true)}
                                        download={openNode.name}
                                        aria-label={`Download ${openNode.name}`}
                                        title="Download"
                                        className={buttonStyles({ variant: 'secondary', iconOnly: true })}
                                    >
                                        <Download className="size-4" aria-hidden />
                                    </a>
                                    <IconButton
                                        icon={fullScreen ? Minimize2 : Maximize2}
                                        label={fullScreen ? 'Leave full screen' : 'Full screen'}
                                        onClick={() => setFullScreen(f => !f)}
                                    />
                                    <IconButton icon={Trash2} tone="danger" label={`Delete ${openNode.name}`} onClick={() => setDeleteTarget(openNode)} />
                                </>
                            }
                        />

                        {docHere && (() => {
                            const kb = knowledge?.baseHolding(openNode.path);
                            return (
                                <div className="flex flex-wrap items-center gap-2 text-[13px] text-text-muted">
                                    <Library className="size-4 shrink-0 text-text-faint" aria-hidden />
                                    <span>In {kb?.name ?? 'a knowledge base'}:</span>
                                    <DocBadge doc={docHere} />
                                    {docHere.status_reason && <span className="truncate">{REASONS[docHere.status_reason] ?? docHere.status_reason}</span>}
                                    <Link href="/knowledge" className="ml-auto shrink-0 underline hover:text-text">Open in Knowledge</Link>
                                </div>
                            );
                        })()}

                        {loadingFile ? (
                            <div className="flex items-center gap-2 py-10 text-sm text-text-faint" role="status">
                                <Spinner size={16} /> Reading…
                            </div>
                        ) : openKind === 'image' ? (
                            <ImageView key={openNode.path} src={rawUrl(openNode.path, storageSource)} name={openNode.name} />
                        ) : openKind === 'pdf' ? (
                            <DownloadOnly title="A PDF" href={rawUrl(openNode.path, storageSource, true)} name={openNode.name}>
                                PDFs open in your own PDF viewer: download it. Agents can still reference it with @[path].
                            </DownloadOnly>
                        ) : binary ? (
                            <DownloadOnly title="Not a text file" href={rawUrl(openNode.path, storageSource, true)} name={openNode.name}>
                                This file can&apos;t be opened here. Download it, or reference it in a prompt with @[path].
                            </DownloadOnly>
                        ) : tooLarge ? (
                            <DownloadOnly title="Too large to open here" href={rawUrl(openNode.path, storageSource, true)} name={openNode.name}>
                                {tooLarge}
                            </DownloadOnly>
                        ) : openKind === 'markdown' ? (
                            <MarkdownEditor
                                key={openNode.path}
                                aria-label="File contents"
                                value={fileContent}
                                onChange={setFileContent}
                                textareaClassName={fullScreen ? 'h-[calc(100vh-11rem)] resize-none' : EDITOR_HEIGHT}
                                placeholder={"# Title\n\nStart writing..."}
                                previewEmpty="Empty file"
                                frontMatter
                                disabled={readOnly}
                                defaultTab={readOnly ? 'preview' : undefined}
                            />
                        ) : (() => {
                            const editor = (
                                <CodeEditor
                                    key={openNode.path}
                                    aria-label="File contents"
                                    value={fileContent}
                                    onChange={setFileContent}
                                    language={openKind === 'json' ? 'json' : codeLanguage(openNode.ext)}
                                    lint={openKind === 'json' ? 'json' : undefined}
                                    format={openKind === 'json' && !readOnly}
                                    readOnly={readOnly}
                                    placeholder="Start typing…"
                                    className={fullScreen ? CODE_HEIGHT_FULL : CODE_HEIGHT}
                                />
                            );
                            return openKind === 'table'
                                ? <TableView key={openNode.path} ext={openNode.ext ?? '.csv'} text={fileContent} editor={editor} />
                                : editor;
                        })()}
                    </>
                )}
            </section>
            </div>
            )}

            {/* Modals */}
            {createDialog && (
                <CreateDialog
                    type={createDialog.type}
                    parentPath={contextFolder}
                    onClose={() => setCreateDialog(null)}
                    onCreate={handleCreate}
                />
            )}
            {deleteTarget && (
                <DeleteConfirm
                    node={deleteTarget}
                    onConfirm={handleDelete}
                    onCancel={() => setDeleteTarget(null)}
                />
            )}
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </div>
    );
}
