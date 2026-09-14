/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';
import React, { useRef, useState, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { FileText, FileJson, AlignLeft } from 'lucide-react';

import type { MentionSource } from '@/components/ui';

interface VaultFile {
    name: string;
    path: string;
    ext: string;
}

interface VaultTextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
    value: string;
    onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
}

/**
 * `@` in the kit's editors: the same vault search and the same `@[path]` insert
 * as `VaultTextarea` below, as a source the markdown editor's suggestion list
 * reads. Cloud's copy of this file exports none until it has a vault.
 */
export const vaultMentions: MentionSource = {
    trigger: '@',
    search: async (query) => {
        const response = await fetch(`/api/vault/search?q=${encodeURIComponent(query)}`);
        if (!response.ok) return [];
        const data = await response.json();
        const files: VaultFile[] = Array.isArray(data.files) ? data.files : [];
        return files.map((file) => ({ id: file.path, label: file.path, detail: file.ext, insert: `@[${file.path}]` }));
    },
};

/**
 * Drop-in replacement for <textarea> with @-mention support for vault files.
 * The dropdown is portaled to document.body (position: fixed) so it is never
 * clipped by ancestor overflow:hidden containers.
 */
export function VaultTextarea({ value, onChange, className, ...rest }: VaultTextareaProps) {
    const textareaRef = useRef<HTMLTextAreaElement>(null);

    const [active, setActive] = useState(false);
    const [query, setQuery] = useState('');
    const [triggerPos, setTriggerPos] = useState(-1);  // '@' index in value string
    const [results, setResults] = useState<VaultFile[]>([]);
    const [loading, setLoading] = useState(false);
    const [selectedIndex, setSelectedIndex] = useState(0);

    // Fixed viewport position of the dropdown — anchored ABOVE the textarea
    const [dropPos, setDropPos] = useState({ bottom: 0, left: 0, width: 0 });

    // -------------------------------------------------------------------------
    // Fetch vault files when query changes
    // -------------------------------------------------------------------------
    useEffect(() => {
        if (!active) return;
        let cancelled = false;
        setLoading(true);

        fetch(`/api/vault/search?q=${encodeURIComponent(query)}`)
            .then(r => r.ok ? r.json() : { files: [] })
            .then(data => {
                if (!cancelled) {
                    setResults(Array.isArray(data.files) ? data.files : []);
                    setSelectedIndex(0);
                    setLoading(false);
                }
            })
            .catch(() => { if (!cancelled) { setResults([]); setLoading(false); } });

        return () => { cancelled = true; };
    }, [active, query]);

    // -------------------------------------------------------------------------
    // Position dropdown below the textarea (fixed, so no clip issues)
    // -------------------------------------------------------------------------
    const positionDropdown = useCallback(() => {
        const ta = textareaRef.current;
        if (!ta) return;
        const rect = ta.getBoundingClientRect();
        setDropPos({
            bottom: window.innerHeight - rect.top + 4,   // pop up above the textarea
            left: rect.left,
            width: rect.width,
        });
    }, []);

    // -------------------------------------------------------------------------
    // Close helpers
    // -------------------------------------------------------------------------
    const close = useCallback(() => {
        setActive(false);
        setQuery('');
        setTriggerPos(-1);
        setResults([]);
        setSelectedIndex(0);
    }, []);

    // -------------------------------------------------------------------------
    // Insert selected file path
    // -------------------------------------------------------------------------
    const insertMention = useCallback((file: VaultFile) => {
        const ta = textareaRef.current;
        if (!ta || triggerPos < 0) return;

        const cursorPos = ta.selectionStart ?? value.length;
        const before = value.slice(0, triggerPos);
        const after = value.slice(cursorPos);
        const insertion = `@[${file.path}]`;
        const newValue = before + insertion + after;
        const newCursor = triggerPos + insertion.length;

        // Update React controlled value via synthetic change event
        const nativeSet = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
        if (nativeSet) {
            nativeSet.call(ta, newValue);
            ta.dispatchEvent(new Event('input', { bubbles: true }));
        }
        onChange({ target: ta } as React.ChangeEvent<HTMLTextAreaElement>);

        setTimeout(() => {
            ta.setSelectionRange(newCursor, newCursor);
            ta.focus();
        }, 0);

        close();
    }, [triggerPos, value, onChange, close]);

    // -------------------------------------------------------------------------
    // Keyboard handler
    // -------------------------------------------------------------------------
    const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (!active) {
            (rest as any).onKeyDown?.(e);
            return;
        }
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            setSelectedIndex(i => Math.min(i + 1, results.length - 1));
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            setSelectedIndex(i => Math.max(i - 1, 0));
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            if (results.length > 0) {
                e.preventDefault();
                insertMention(results[selectedIndex]);
            } else {
                close();
            }
        } else if (e.key === 'Escape') {
            e.preventDefault();
            close();
        } else {
            (rest as any).onKeyDown?.(e);
        }
    }, [active, results, selectedIndex, insertMention, close, rest]);

    // -------------------------------------------------------------------------
    // Change handler — detect @ trigger
    // -------------------------------------------------------------------------
    const handleChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
        const newValue = e.target.value;
        const cursorPos = e.target.selectionStart ?? newValue.length;
        const textBefore = newValue.slice(0, cursorPos);

        // Match last @ not yet closed by ]
        const match = textBefore.match(/@([^@[\]\n]*)$/);
        if (match) {
            const q = match[1];
            const tPos = cursorPos - q.length - 1; // index of '@'
            setActive(true);
            setQuery(q);
            setTriggerPos(tPos);
            setSelectedIndex(0);
            positionDropdown();
        } else {
            if (active) close();
        }

        onChange(e);
    }, [active, onChange, close, positionDropdown]);

    // -------------------------------------------------------------------------
    // Close on outside click
    // -------------------------------------------------------------------------
    useEffect(() => {
        if (!active) return;
        const handler = (e: MouseEvent) => {
            const ta = textareaRef.current;
            if (ta && !ta.contains(e.target as Node)) {
                const dropdown = document.getElementById('vault-mention-dropdown');
                if (!dropdown || !dropdown.contains(e.target as Node)) {
                    close();
                }
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [active, close]);

    // -------------------------------------------------------------------------
    // Reposition on scroll/resize while open
    // -------------------------------------------------------------------------
    useEffect(() => {
        if (!active) return;
        const update = () => positionDropdown();
        window.addEventListener('scroll', update, true);
        window.addEventListener('resize', update);
        return () => {
            window.removeEventListener('scroll', update, true);
            window.removeEventListener('resize', update);
        };
    }, [active, positionDropdown]);

    // -------------------------------------------------------------------------
    // Dropdown (portaled to body)
    // -------------------------------------------------------------------------
    const dropdown = active ? createPortal(
        <div
            id="vault-mention-dropdown"
            style={{ bottom: dropPos.bottom, left: dropPos.left, width: Math.max(dropPos.width, 288) }}
            className="fixed z-[9999] bg-zinc-900 border border-zinc-700 shadow-2xl rounded-md overflow-hidden"
        >
            {/* Header */}
            <div className="px-3 py-1.5 text-[10px] text-zinc-500 border-b border-zinc-800 font-mono flex items-center gap-1.5">
                <span className="text-emerald-400">@</span> Vault file reference
                {query && <span className="text-zinc-600">— &ldquo;{query}&rdquo;</span>}
            </div>

            {loading ? (
                <div className="px-3 py-3 text-xs text-zinc-500 text-center">Searching…</div>
            ) : results.length === 0 ? (
                <div className="px-3 py-3 text-xs text-zinc-600 text-center">No vault files found</div>
            ) : (
                <div className="max-h-52 overflow-y-auto">
                    {results.map((file, idx) => (
                        <button
                            key={file.path}
                            type="button"
                            onMouseDown={(e) => {
                                e.preventDefault();
                                insertMention(file);
                            }}
                            className={`w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-zinc-800 transition-colors ${idx === selectedIndex ? 'bg-zinc-800' : ''}`}
                        >
                            {file.ext === '.json'
                                ? <FileJson className="h-3.5 w-3.5 text-amber-400 flex-shrink-0" />
                                : file.ext === '.txt'
                                    ? <AlignLeft className="h-3.5 w-3.5 text-zinc-400 flex-shrink-0" />
                                    : <FileText className="h-3.5 w-3.5 text-blue-400 flex-shrink-0" />
                            }
                            <div className="min-w-0 flex-1">
                                <div className="text-xs font-medium text-zinc-200 truncate">{file.name}</div>
                                <div className="text-[10px] text-zinc-500 truncate font-mono">{file.path}</div>
                            </div>
                            <span className={`text-[9px] px-1 py-0.5 rounded-md font-mono flex-shrink-0 ${
                                file.ext === '.json' ? 'bg-amber-900/40 text-amber-400'
                                : file.ext === '.txt' ? 'bg-zinc-800 text-zinc-400'
                                : 'bg-blue-900/40 text-blue-400'
                            }`}>
                                {file.ext.slice(1)}
                            </span>
                        </button>
                    ))}
                </div>
            )}

            <div className="px-3 py-1.5 border-t border-zinc-800 text-[9px] text-zinc-600 font-mono">
                ↑↓ navigate · Enter/Tab select · Esc cancel
            </div>
        </div>,
        document.body
    ) : null;

    return (
        <>
            <textarea
                ref={textareaRef}
                value={value}
                onChange={handleChange}
                onKeyDown={handleKeyDown}
                className={className}
                {...rest}
            />
            {dropdown}
        </>
    );
}
