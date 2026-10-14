/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';
import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { FolderOpen, Folder, FileText, FileJson, ChevronRight, ChevronDown, Plus, Trash2, RefreshCw, FolderPlus, FilePlus, AlignLeft, Cloud, Upload, Library } from 'lucide-react';
import { DetailHead, LIST_DETAIL, ROW_CURRENT, showDetail } from '@/components/run';
import {
    Badge, Button, ConfirmDialog, EmptyState, ErrorNote, Field, IconButton, Input, MarkdownEditor, Modal, Segmented,
    Spinner, Textarea, Toast, useToast,
} from '@/components/ui';
import { cn } from '@/lib/cn';
import { DocBadge, useVaultKnowledge, vaultKnowledge, type VaultKnowledge } from '@/components/knowledge/VaultKnowledge';
import { ACCEPTED, REASONS, uploadToKnowledge, useKnowledgeEnabled, type KnowledgeBase } from '@/lib/knowledge';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface VaultNode {
    name: string;
    path: string;
    type: 'file' | 'folder';
    ext?: string;
    children?: VaultNode[];
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
    return <FileText className={className} aria-hidden />;
}

const KIND_WORD: Record<string, string> = { '.json': 'JSON', '.txt': 'Plain text' };

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
    const [fileType, setFileType] = useState<'md' | 'json' | 'txt'>('md');

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

function JsonEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
    const [error, setError] = useState<string | null>(null);

    const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
        const v = e.target.value;
        onChange(v);
        try {
            JSON.parse(v);
            setError(null);
        } catch (err: any) {
            setError(err.message);
        }
    };

    return (
        <div className="grid gap-2">
            <Textarea
                aria-label="File contents"
                value={value}
                onChange={handleChange}
                spellCheck={false}
                invalid={!!error}
                className={cn('font-code text-[13px] leading-relaxed', EDITOR_HEIGHT)}
            />
            {error && <ErrorNote>{error}</ErrorNote>}
        </div>
    );
}

function PlainTextEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
    return (
        <Textarea
            aria-label="File contents"
            value={value}
            onChange={e => onChange(e.target.value)}
            spellCheck={false}
            className={cn('font-code text-[13px] leading-relaxed', EDITOR_HEIGHT)}
            placeholder="Start typing..."
        />
    );
}

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
    useEffect(() => {
        if (!openPath) return;
        setLoadingFile(true);
        setBinary(false);
        fetch(`/api/vault/file?path=${encodeURIComponent(openPath)}&source=${storageSource}`)
            .then(r => {
                // 415: a PDF, an image — nothing an editor can open (F323).
                if (r.status === 415) setBinary(true);
                return r.ok ? r.json() : null;
            })
            .then(data => {
                const c = data?.content ?? '';
                setFileContent(c);
                setSavedContent(c);
            })
            .catch(() => {
                setFileContent('');
                setSavedContent('');
            })
            .finally(() => setLoadingFile(false));
    }, [openPath, storageSource, reads]);

    const handleSave = async () => {
        if (!openNode) return;
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
            } else {
                show(`Could not save ${openNode.name}`, 'danger');
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
                    Create <span className="font-code">.md</span>, <span className="font-code">.json</span>, or <span className="font-code">.txt</span> files
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

                <div className="max-h-[calc(100vh-17rem)] overflow-y-auto modern-scrollbar max-lg:max-h-80">
                    {loading ? (
                        <div className="flex items-center gap-2 px-2 py-4 text-sm text-text-faint" role="status">
                            <Spinner size={14} /> Loading…
                        </div>
                    ) : (
                        tree.map(node => (
                            <TreeNode
                                key={node.path}
                                node={node}
                                selectedPath={openNode?.path ?? null}
                                onSelect={(n) => {
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
                                }}
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
            <section ref={detailRef} aria-label="File" className="grid min-w-0 scroll-mt-4 content-start gap-4">
                {openNode && (
                    <>
                        <DetailHead
                            leading={<span className="mt-1 text-text-faint"><FileIcon node={openNode} className="size-5" /></span>}
                            title={openNode.name}
                            sub={<><span>{KIND_WORD[openNode.ext ?? ''] ?? 'Markdown'}</span> · <span className="font-code">{openNode.path}</span></>}
                            actions={
                                <>
                                    {isDirty && (
                                        <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xs font-medium text-text-muted">Unsaved</span>
                                    )}
                                    <Button onClick={handleSave} disabled={savingFile || !isDirty || binary} loading={savingFile}>
                                        Save
                                    </Button>
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
                        ) : binary ? (
                            <EmptyState title="Not a text file">
                                This file can&apos;t be opened here. Agents can still reference it with @[path].
                            </EmptyState>
                        ) : openNode.ext === '.json' ? (
                            <JsonEditor key={openNode.path} value={fileContent} onChange={setFileContent} />
                        ) : openNode.ext === '.txt' ? (
                            <PlainTextEditor value={fileContent} onChange={setFileContent} />
                        ) : (
                            <MarkdownEditor
                                key={openNode.path}
                                aria-label="File contents"
                                value={fileContent}
                                onChange={setFileContent}
                                textareaClassName={EDITOR_HEIGHT}
                                placeholder={"# Title\n\nStart writing..."}
                                previewEmpty="Empty file"
                                frontMatter
                            />
                        )}
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
