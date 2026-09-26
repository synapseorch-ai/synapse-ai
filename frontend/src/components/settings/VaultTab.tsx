/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';
import React, { useState, useEffect, useCallback } from 'react';
import { FolderOpen, Folder, FileText, FileJson, ChevronRight, ChevronDown, Plus, Trash2, Save, Eye, EyeOff, RefreshCw, FolderPlus, FilePlus, AlignLeft, Cloud } from 'lucide-react';
import { renderTextContent } from '@/lib/utils';
import { ConfirmDialog, Modal, Spinner } from '@/components/ui';

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
    depth = 0,
}: {
    node: VaultNode;
    selectedPath: string | null;
    onSelect: (node: VaultNode) => void;
    onDelete: (node: VaultNode) => void;
    onCreateInFolder: (folderPath: string) => void;
    depth?: number;
}) {
    const [expanded, setExpanded] = useState(depth === 0);

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
                className={`group flex items-center gap-1 pr-2 transition-colors rounded-md text-xs
                    ${isSelected ? 'bg-zinc-800 text-white' : 'text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800/50'}`}
            >
                <button
                    type="button"
                    onClick={handleClick}
                    aria-expanded={isFolder ? expanded : undefined}
                    aria-current={isSelected ? 'true' : undefined}
                    className="flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left rounded-md"
                    style={{ paddingLeft: `${8 + depth * 16}px` }}
                >
                    {/* Expand/collapse arrow for folders */}
                    {isFolder ? (
                        <span className="text-text-faint flex-shrink-0">
                            {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                        </span>
                    ) : (
                        <span className="w-3 flex-shrink-0" />
                    )}

                    {/* Icon */}
                    {isFolder ? (
                        expanded
                            ? <FolderOpen className="h-3.5 w-3.5 text-amber-400 flex-shrink-0" />
                            : <Folder className="h-3.5 w-3.5 text-amber-400 flex-shrink-0" />
                    ) : node.ext === '.json' ? (
                        <FileJson className="h-3.5 w-3.5 text-amber-300 flex-shrink-0" />
                    ) : node.ext === '.txt' ? (
                        <AlignLeft className="h-3.5 w-3.5 text-zinc-400 flex-shrink-0" />
                    ) : (
                        <FileText className="h-3.5 w-3.5 text-blue-400 flex-shrink-0" />
                    )}

                    <span className="flex-1 truncate font-mono">{node.name}</span>
                </button>

                {/* Folder actions: + new file, delete */}
                {isFolder && (
                    <button
                        type="button"
                        onClick={() => onCreateInFolder(node.path)}
                        className="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 text-text-faint hover:text-emerald-400 transition-all p-0.5 flex-shrink-0"
                        aria-label={`New file in ${node.name}`}
                        title={`New file in ${node.name}`}
                    >
                        <FilePlus className="h-3 w-3" />
                    </button>
                )}
                <button
                    type="button"
                    onClick={() => onDelete(node)}
                    className="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 text-text-faint hover:text-red-500 transition-opacity p-0.5 flex-shrink-0"
                    aria-label={`Delete ${node.name}`}
                    title={`Delete ${node.name}`}
                >
                    <Trash2 className="h-3 w-3" />
                </button>
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
                            depth={depth + 1}
                        />
                    ))}
                </div>
            )}
            {isFolder && expanded && (!node.children || node.children.length === 0) && (
                <div
                    className="text-[10px] text-text-faint italic"
                    style={{ paddingLeft: `${8 + (depth + 2) * 16}px`, paddingTop: '4px', paddingBottom: '4px' }}
                >
                    Empty folder
                </div>
            )}
        </div>
    );
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
        >
                <form onSubmit={handleSubmit} className="p-4 space-y-4">
                    {parentPath && (
                        <p className="text-[10px] text-text-faint font-mono">
                            Location: <span className="text-zinc-400">/{parentPath}</span>
                        </p>
                    )}

                    {type === 'file' && (
                        <div className="flex gap-2">
                            <button
                                type="button"
                                onClick={() => setFileType('md')}
                                className={`flex-1 flex items-center justify-center gap-2 py-2.5 text-xs font-bold border transition-colors ${fileType === 'md' ? 'border-blue-500 bg-blue-950/40 text-blue-400' : 'border-zinc-700 text-text-faint hover:border-zinc-600'} rounded-md`}
                            >
                                <FileText className="h-3.5 w-3.5" /> Markdown
                            </button>
                            <button
                                type="button"
                                onClick={() => setFileType('json')}
                                className={`flex-1 flex items-center justify-center gap-2 py-2.5 text-xs font-bold border transition-colors ${fileType === 'json' ? 'border-amber-500 bg-amber-950/40 text-amber-400' : 'border-zinc-700 text-text-faint hover:border-zinc-600'} rounded-md`}
                            >
                                <FileJson className="h-3.5 w-3.5" /> JSON
                            </button>
                            <button
                                type="button"
                                onClick={() => setFileType('txt')}
                                className={`flex-1 flex items-center justify-center gap-2 py-2.5 text-xs font-bold border transition-colors ${fileType === 'txt' ? 'border-zinc-400 bg-zinc-800/60 text-zinc-200' : 'border-zinc-700 text-text-faint hover:border-zinc-600'} rounded-md`}
                            >
                                <AlignLeft className="h-3.5 w-3.5" /> Text
                            </button>
                        </div>
                    )}

                    <div className="space-y-1">
                        <label htmlFor="vault-type-file-file-name-folder-n" className="text-[10px] font-bold text-text-faint uppercase">
                            {type === 'file' ? 'File Name' : 'Folder Name'}
                        </label>
                        <input
                            id="vault-type-file-file-name-folder-n"
                            autoFocus
                            type="text"
                            value={name}
                            onChange={e => setName(e.target.value)}
                            placeholder={type === 'file' ? `my-document.${fileType}` : 'my-folder'}
                            className="w-full bg-zinc-900 border border-zinc-700 px-3 py-2 text-xs text-white font-mono focus:border-white focus:outline-none rounded-md"
                        />
                        {type === 'file' && (
                            <p className="text-[9px] text-text-faint">
                                Extension <span className="text-zinc-400">.{fileType}</span> will be appended automatically if not included.
                            </p>
                        )}
                    </div>

                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={onClose}
                            className="flex-1 py-2 text-xs text-text-faint hover:text-white border border-zinc-700 hover:border-zinc-500 transition-colors"
                        >
                            Cancel
                        </button>
                        <button
                            type="submit"
                            disabled={!name.trim()}
                            className="flex-1 py-2 text-xs font-bold bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            Create
                        </button>
                    </div>
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
// JSON Editor with validation
// ---------------------------------------------------------------------------

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
        <div className="flex-1 flex flex-col min-h-0">
            <textarea
                value={value}
                onChange={handleChange}
                spellCheck={false}
                className={`flex-1 min-h-0 w-full bg-zinc-950 p-4 text-xs font-mono text-zinc-200 focus:outline-none resize-none leading-relaxed border-0 ${error ? 'border-b-2 border-red-600/60' : ''} rounded-md`}
                style={{ fontFamily: 'monospace' }}
            />
            {error && (
                <div className="px-4 py-1.5 text-[10px] text-red-400 font-mono bg-red-950/30 border-t border-red-900/50 flex-shrink-0">
                    ⚠ {error}
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Markdown Editor + Preview
// ---------------------------------------------------------------------------

function MarkdownEditor({
    value,
    onChange,
}: {
    value: string;
    onChange: (v: string) => void;
}) {
    const [showPreview, setShowPreview] = useState(false);

    return (
        <div className="flex-1 flex flex-col min-h-0">
            <div className="flex items-center justify-between px-4 py-2 border-b border-zinc-800 flex-shrink-0">
                <div className="flex items-center gap-1.5">
                    <FileText className="h-3.5 w-3.5 text-blue-400" />
                    <span className="text-[10px] font-bold text-zinc-400 uppercase">Markdown</span>
                </div>
                <button
                    onClick={() => setShowPreview(v => !v)}
                    className="flex items-center gap-1.5 text-[10px] font-bold text-text-faint hover:text-white transition-colors px-2 py-1"
                >
                    {showPreview ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                    {showPreview ? 'EDIT' : 'PREVIEW'}
                </button>
            </div>
            {showPreview ? (
                <div className="flex-1 min-h-0 overflow-y-auto p-4 text-sm text-zinc-300 leading-relaxed modern-scrollbar">
                    {renderTextContent(value || '*Empty file*')}
                </div>
            ) : (
                <textarea
                    value={value}
                    onChange={e => onChange(e.target.value)}
                    spellCheck={false}
                    className="flex-1 min-h-0 w-full bg-zinc-950 p-4 text-xs font-mono text-zinc-200 focus:outline-none resize-none leading-relaxed border-0 rounded-md"
                    style={{ fontFamily: 'monospace' }}
                    placeholder="# Title&#10;&#10;Start writing..."
                />
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Plain Text Editor
// ---------------------------------------------------------------------------

function PlainTextEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
    return (
        <div className="flex-1 flex flex-col min-h-0">
            <div className="flex items-center gap-1.5 px-4 py-2 border-b border-zinc-800 flex-shrink-0">
                <AlignLeft className="h-3.5 w-3.5 text-zinc-400" />
                <span className="text-[10px] font-bold text-zinc-400 uppercase">Plain Text</span>
            </div>
            <textarea
                value={value}
                onChange={e => onChange(e.target.value)}
                spellCheck={false}
                className="flex-1 min-h-0 w-full bg-zinc-950 p-4 text-xs font-mono text-zinc-200 focus:outline-none resize-none leading-relaxed border-0 rounded-md"
                style={{ fontFamily: 'monospace' }}
                placeholder="Start typing..."
            />
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main VaultTab Component
// ---------------------------------------------------------------------------

export function VaultTab() {
    const [tree, setTree] = useState<VaultNode[]>([]);
    const [loading, setLoading] = useState(true);
    const [selectedNode, setSelectedNode] = useState<VaultNode | null>(null);
    const [fileContent, setFileContent] = useState('');
    const [savedContent, setSavedContent] = useState('');
    const [loadingFile, setLoadingFile] = useState(false);
    const [savingFile, setSavingFile] = useState(false);
    const [saveStatus, setSaveStatus] = useState<'idle' | 'saved' | 'error'>('idle');

    // Dialogs
    const [createDialog, setCreateDialog] = useState<{ type: 'file' | 'folder'; parentPath: string } | null>(null);
    const [deleteTarget, setDeleteTarget] = useState<VaultNode | null>(null);

    // Context for right-click / folder selection for new items
    const [contextFolder, setContextFolder] = useState<string>('');

    const [s3Bucket, setS3Bucket] = useState<string>('');
    const [storageSource, setStorageSource] = useState<'s3' | 'local'>('s3');

    const fetchTree = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch(`/api/vault/tree?source=${storageSource}`);
            if (res.ok) {
                const data = await res.json();
                setTree(data.tree || []);
            }
        } catch (e) {
            console.error('Failed to load vault tree', e);
        } finally {
            setLoading(false);
        }
    }, [storageSource]);

    useEffect(() => { fetchTree(); }, [fetchTree]);

    // Fetch scale config to show S3 banner when configured
    useEffect(() => {
        fetch('/api/scale/config')
            .then(r => r.ok ? r.json() : null)
            .then(data => { if (data?.s3_bucket) setS3Bucket(data.s3_bucket); })
            .catch(() => {});
    }, []);

    // Load file content when a file is selected
    useEffect(() => {
        if (!selectedNode || selectedNode.type !== 'file') return;
        setLoadingFile(true);
        setSaveStatus('idle');
        fetch(`/api/vault/file?path=${encodeURIComponent(selectedNode.path)}&source=${storageSource}`)
            .then(r => r.ok ? r.json() : null)
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
    }, [selectedNode, storageSource]);

    const handleSave = async () => {
        if (!selectedNode || selectedNode.type !== 'file') return;
        setSavingFile(true);
        setSaveStatus('idle');
        try {
            const res = await fetch('/api/vault/file', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: selectedNode.path, content: fileContent, source: storageSource }),
            });
            if (res.ok) {
                setSavedContent(fileContent);
                setSaveStatus('saved');
                setTimeout(() => setSaveStatus('idle'), 2000);
            } else {
                setSaveStatus('error');
            }
        } catch {
            setSaveStatus('error');
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
            }
        } catch (e) {
            console.error('Failed to create', e);
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
            }
        } catch (e) {
            console.error('Failed to delete', e);
        } finally {
            setDeleteTarget(null);
        }
    };

    const isDirty = fileContent !== savedContent;

    return (
        <div className="flex-1 flex flex-col min-h-0 overflow-hidden">
            {/* S3 storage banner with source toggle */}
            {s3Bucket && (
                <div className="flex items-center gap-2 px-4 py-2 bg-zinc-900 border-b border-zinc-800 text-xs text-zinc-400 shrink-0">
                    <Cloud className="h-3.5 w-3.5 text-sky-400 shrink-0" />
                    <span>
                        <span className="text-sky-400 font-semibold">S3 Connected</span>
                        {' — '}bucket: <span className="font-mono text-zinc-300">{s3Bucket}</span>
                    </span>
                    <div className="ml-auto flex items-center gap-1.5">
                        <span className="text-[10px] text-text-faint">View:</span>
                        <button
                            onClick={() => { setStorageSource('s3'); setSelectedNode(null); setFileContent(''); setSavedContent(''); }}
                            className={`px-2 py-0.5 text-[10px] font-bold border transition-colors ${storageSource === 's3' ? 'border-sky-500 bg-sky-950/40 text-sky-400' : 'border-zinc-700 text-text-faint hover:border-zinc-600 hover:text-zinc-300'} rounded-md`}
                        >
                            S3
                        </button>
                        <button
                            onClick={() => { setStorageSource('local'); setSelectedNode(null); setFileContent(''); setSavedContent(''); }}
                            className={`px-2 py-0.5 text-[10px] font-bold border transition-colors ${storageSource === 'local' ? 'border-amber-500 bg-amber-950/40 text-amber-400' : 'border-zinc-700 text-text-faint hover:border-zinc-600 hover:text-zinc-300'} rounded-md`}
                        >
                            Local
                        </button>
                    </div>
                </div>
            )}

            <div className="flex-1 flex min-h-0 overflow-hidden">
            {/* Left: Tree sidebar */}
            <div className="w-64 flex-shrink-0 border-r border-zinc-800 flex flex-col bg-zinc-950">
                {/* Toolbar */}
                <div className="flex items-center gap-1 px-3 py-2.5 border-b border-zinc-800">
                    <span className="text-[10px] font-bold text-text-faint uppercase flex-1">Vault Files</span>
                    <button
                        onClick={() => { setContextFolder(''); setCreateDialog({ type: 'folder', parentPath: '' }); }}
                        title="New Folder"
                        className="p-1 text-text-faint hover:text-white hover:bg-zinc-800 transition-colors"
                    >
                        <FolderPlus className="h-3.5 w-3.5" />
                    </button>
                    <button
                        onClick={() => { setContextFolder(''); setCreateDialog({ type: 'file', parentPath: '' }); }}
                        title="New File"
                        className="p-1 text-text-faint hover:text-white hover:bg-zinc-800 transition-colors"
                    >
                        <FilePlus className="h-3.5 w-3.5" />
                    </button>
                    <button
                        onClick={fetchTree}
                        title="Refresh"
                        className="p-1 text-text-faint hover:text-white hover:bg-zinc-800 transition-colors"
                    >
                        <RefreshCw className="h-3 w-3" />
                    </button>
                </div>

                {/* Tree */}
                <div className="flex-1 overflow-y-auto modern-scrollbar py-1">
                    {loading ? (
                        <div className="flex items-center gap-2 px-4 py-4 text-text-faint text-xs">
                            <Spinner size={14} /> Loading…
                        </div>
                    ) : tree.length === 0 ? (
                        <div className="px-4 py-6 text-center">
                            <div className="text-text-faint text-xs mb-3">Vault is empty</div>
                            <button
                                onClick={() => { setContextFolder(''); setCreateDialog({ type: 'file', parentPath: '' }); }}
                                className="text-[10px] text-zinc-400 hover:text-white underline"
                            >
                                Create your first file →
                            </button>
                        </div>
                    ) : (
                        tree.map(node => (
                            <TreeNode
                                key={node.path}
                                node={node}
                                selectedPath={selectedNode?.path ?? null}
                                onSelect={(n) => {
                                    setSelectedNode(n);
                                    // Set context folder for "new" actions
                                    if (n.type === 'folder') {
                                        setContextFolder(n.path);
                                    } else {
                                        const parts = n.path.split('/');
                                        setContextFolder(parts.slice(0, -1).join('/'));
                                    }
                                }}
                                onDelete={setDeleteTarget}
                                onCreateInFolder={(folderPath) => {
                                    setContextFolder(folderPath);
                                    setCreateDialog({ type: 'file', parentPath: folderPath });
                                }}
                            />
                        ))
                    )}
                </div>

                {/* Bottom hint */}
                <div className="px-3 py-2 border-t border-zinc-800">
                    <p className="text-[9px] text-text-faint leading-relaxed">
                        Files here can be referenced in agent prompts using <span className="font-mono text-text-faint">@[path]</span>
                    </p>
                </div>
            </div>

            {/* Right: Content area */}
            <div className="flex-1 flex flex-col min-h-0 bg-zinc-950">
                {selectedNode && selectedNode.type === 'file' ? (
                    <>
                        {/* File header */}
                        <div className="flex items-center gap-3 px-4 py-2.5 border-b border-zinc-800 flex-shrink-0">
                            {selectedNode.ext === '.json'
                                ? <FileJson className="h-4 w-4 text-amber-400 flex-shrink-0" />
                                : selectedNode.ext === '.txt'
                                    ? <AlignLeft className="h-4 w-4 text-zinc-400 flex-shrink-0" />
                                    : <FileText className="h-4 w-4 text-blue-400 flex-shrink-0" />
                            }
                            <span className="text-xs font-mono text-zinc-300 flex-1 truncate">{selectedNode.path}</span>
                            {isDirty && <span className="text-[10px] text-amber-400">unsaved changes</span>}
                            <button
                                onClick={handleSave}
                                disabled={savingFile || !isDirty}
                                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-bold transition-colors disabled:opacity-40 disabled:cursor-not-allowed
                                    ${saveStatus === 'saved' ? 'bg-emerald-600 text-white' : saveStatus === 'error' ? 'bg-red-600 text-white' : 'bg-accent text-accent-fg hover:bg-accent-hover'}`}
                            >
                                {savingFile ? <Spinner size={12} /> : <Save className="h-3 w-3" />}
                                {saveStatus === 'saved' ? 'SAVED' : saveStatus === 'error' ? 'ERROR' : 'SAVE'}
                            </button>
                        </div>

                        {/* Editor area */}
                        {loadingFile ? (
                            <div className="flex-1 flex items-center justify-center">
                                <Spinner size={20} className="text-text-faint" />
                            </div>
                        ) : selectedNode.ext === '.json' ? (
                            <JsonEditor value={fileContent} onChange={setFileContent} />
                        ) : selectedNode.ext === '.txt' ? (
                            <PlainTextEditor value={fileContent} onChange={setFileContent} />
                        ) : (
                            <MarkdownEditor value={fileContent} onChange={setFileContent} />
                        )}
                    </>
                ) : (
                    <div className="flex-1 flex flex-col items-center justify-center gap-6 text-center p-8">
                        <div className="space-y-2">
                            <div className="h-16 w-16 mx-auto rounded-full bg-zinc-900 border border-zinc-800 flex items-center justify-center">
                                <FolderOpen className="h-7 w-7 text-text-faint" />
                            </div>
                            <h3 className="text-sm font-bold text-zinc-400">Select a file to edit</h3>
                            <p className="text-xs text-text-faint max-w-xs">
                                Use the file explorer on the left to browse your vault. Create <span className="text-zinc-400">.md</span>, <span className="text-zinc-400">.json</span>, or <span className="text-zinc-400">.txt</span> files to use as context in agent prompts.
                            </p>
                        </div>
                        <div className="flex gap-3">
                            <button
                                onClick={() => { setContextFolder(''); setCreateDialog({ type: 'folder', parentPath: '' }); }}
                                className="flex items-center gap-2 px-4 py-2 text-xs font-bold border border-zinc-700 text-zinc-400 hover:text-white hover:border-zinc-500 transition-colors"
                            >
                                <FolderPlus className="h-3.5 w-3.5" /> New Folder
                            </button>
                            <button
                                onClick={() => { setContextFolder(''); setCreateDialog({ type: 'file', parentPath: '' }); }}
                                className="flex items-center gap-2 px-4 py-2 text-xs font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-colors"
                            >
                                <Plus className="h-3.5 w-3.5" /> New File
                            </button>
                        </div>
                    </div>
                )}
            </div>

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
        </div>
        </div>
    );
}
