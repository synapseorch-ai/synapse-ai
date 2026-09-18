'use client';

/**
 * Undo/redo, dirty tracking and the unload guard for the builder draft.
 *
 * Lives in the shared directory because both products' shells need exactly
 * this and nothing product-specific is in it. The shell swaps its
 * `useState<Orchestration | null>` for this hook; every existing mutator keeps
 * working because `setDraft` has the same shape — it just records history.
 *
 * Two write paths, deliberately:
 * - `setDraft`   — an *edit*: pushes the previous state onto the undo stack.
 *   Edits landing within `coalesceMs` of each other share one undo entry, so
 *   typing a name is one undo, not one per keystroke.
 * - `replaceDraft` — a *navigation*: selecting another orchestration, loading
 *   from the server, or applying a save response. Resets the stacks — undo
 *   must never carry you into a different orchestration's history.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { Orchestration } from '@/types/orchestration';

const MAX_HISTORY = 100;

export function useDraftHistory(coalesceMs = 800) {
    const [draft, setDraftState] = useState<Orchestration | null>(null);
    const [past, setPast] = useState<Orchestration[]>([]);
    const [future, setFuture] = useState<Orchestration[]>([]);
    const [savedJson, setSavedJson] = useState<string>('null');
    const lastPushAtRef = useRef(0);

    const setDraft = useCallback((next: Orchestration | null) => {
        setDraftState((prev) => {
            if (prev && next && prev.id === next.id) {
                const now = Date.now();
                if (now - lastPushAtRef.current > coalesceMs) {
                    lastPushAtRef.current = now;
                    setPast((p) => [...p.slice(-(MAX_HISTORY - 1)), prev]);
                    setFuture([]);
                } else {
                    lastPushAtRef.current = now;
                }
            } else {
                // Different orchestration (or null) through the edit path —
                // treat as navigation so undo cannot cross drafts.
                setPast([]);
                setFuture([]);
            }
            return next;
        });
    }, [coalesceMs]);

    const replaceDraft = useCallback((next: Orchestration | null, opts?: { saved?: boolean }) => {
        setPast([]);
        setFuture([]);
        lastPushAtRef.current = 0;
        setDraftState(next);
        if (opts?.saved || next === null) setSavedJson(JSON.stringify(next));
    }, []);

    const markSaved = useCallback((saved: Orchestration) => {
        setSavedJson(JSON.stringify(saved));
        setDraftState(saved);
    }, []);

    const undo = useCallback(() => {
        setPast((p) => {
            if (p.length === 0) return p;
            const prev = p[p.length - 1];
            setDraftState((current) => {
                if (current) setFuture((f) => [...f, current]);
                return prev;
            });
            lastPushAtRef.current = 0;
            return p.slice(0, -1);
        });
    }, []);

    const redo = useCallback(() => {
        setFuture((f) => {
            if (f.length === 0) return f;
            const next = f[f.length - 1];
            setDraftState((current) => {
                if (current) setPast((p) => [...p, current]);
                return next;
            });
            lastPushAtRef.current = 0;
            return f.slice(0, -1);
        });
    }, []);

    const draftJson = useMemo(() => JSON.stringify(draft), [draft]);
    const dirty = draft !== null && draftJson !== savedJson;

    // Layout work is work too: losing node positions to a closed tab is the
    // silent data loss this guard exists for.
    useEffect(() => {
        if (!dirty) return;
        const handler = (e: BeforeUnloadEvent) => {
            e.preventDefault();
        };
        window.addEventListener('beforeunload', handler);
        return () => window.removeEventListener('beforeunload', handler);
    }, [dirty]);

    return {
        draft,
        setDraft,
        replaceDraft,
        markSaved,
        undo,
        redo,
        canUndo: past.length > 0,
        canRedo: future.length > 0,
        dirty,
    };
}

/**
 * Builder keyboard shortcuts: undo/redo/duplicate/save. Undo, redo and
 * duplicate ignore keystrokes that belong to a focused field — Cmd+Z inside an
 * input is the input's own undo, and CodeMirror owns its keymap entirely.
 *
 * **Save is the exception**: it works from inside a field. Returning early
 * there meant Ctrl+S while typing a name did nothing — and nothing stopped the
 * browser's own "Save page" either (F48). A focused input is blurred first,
 * because some commit on blur (cloud's `InlineEdit`, the panel's
 * `LocalInput`), and the save runs a task later through a ref, so it reads the
 * draft that commit produced rather than the one this handler closed over.
 */
export function useBuilderShortcuts({
    enabled,
    undo,
    redo,
    onDuplicate,
    onSave,
}: {
    enabled: boolean;
    undo: () => void;
    redo: () => void;
    onDuplicate?: () => void;
    onSave?: () => void;
}) {
    const saveRef = useRef(onSave);
    useLayoutEffect(() => {
        saveRef.current = onSave;
    });

    useEffect(() => {
        if (!enabled) return;
        const handler = (e: KeyboardEvent) => {
            const mod = e.metaKey || e.ctrlKey;
            if (!mod) return;
            const key = e.key.toLowerCase();
            if (key === 's' && onSave) {
                e.preventDefault();
                const field = document.activeElement;
                if (field instanceof HTMLElement && field.matches('input, textarea, select')) {
                    field.blur();
                    window.setTimeout(() => {
                        saveRef.current?.();
                        if (field.isConnected) field.focus();
                    }, 0);
                } else {
                    onSave();
                }
                return;
            }
            const target = e.target as HTMLElement | null;
            if (target?.closest('input, textarea, select, [contenteditable="true"], .cm-editor')) return;
            if (key === 'z') {
                e.preventDefault();
                if (e.shiftKey) redo();
                else undo();
            } else if (key === 'y') {
                e.preventDefault();
                redo();
            } else if (key === 'd' && onDuplicate) {
                e.preventDefault();
                onDuplicate();
            }
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, [enabled, undo, redo, onDuplicate, onSave]);
}
