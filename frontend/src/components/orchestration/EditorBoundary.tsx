'use client';

/**
 * A part of the workflow editor that throws says what it could not show, and the rest stays usable (F1054; HOT2,
 * D323).
 *
 * There was no boundary anywhere in the app: one step the canvas could not draw took the whole page to Next's error
 * card, with nothing saying which step or why. Each step on the canvas, the step's card and the workbench sit in one
 * of these. It shows its part again when `resetKey` changes — another step, another workflow, an edit.
 */

import * as React from 'react';
import { AlertCircle } from 'lucide-react';

import { cn } from '@/lib/cn';

/** The reason in words: a production build's React errors are a number and a link, kept in the note's tooltip. */
function saidOf(message: string): string {
    if (/Minified React error #31\b/.test(message)) return 'one of its fields holds an object where text belongs.';
    if (/Minified React error/.test(message)) return 'the editor could not draw it.';
    return message;
}

interface EditorBoundaryProps {
    /** What could not be shown, as a person reads it: “Step “Generate API Keys””, “This step's settings”. */
    what: string;
    resetKey?: unknown;
    /** A step on the canvas: a card the size of a step. Otherwise a note across its space. */
    compact?: boolean;
    /** A way on beside the note (the workbench's Back to workflows). */
    action?: React.ReactNode;
    children: React.ReactNode;
}

interface EditorBoundaryState {
    failed: Error | null;
    key: unknown;
}

export class EditorBoundary extends React.Component<EditorBoundaryProps, EditorBoundaryState> {
    state: EditorBoundaryState = { failed: null, key: this.props.resetKey };

    static getDerivedStateFromError(failed: Error): Partial<EditorBoundaryState> {
        return { failed };
    }

    static getDerivedStateFromProps(props: EditorBoundaryProps, state: EditorBoundaryState): Partial<EditorBoundaryState> | null {
        return Object.is(props.resetKey, state.key) ? null : { failed: null, key: props.resetKey };
    }

    componentDidCatch(error: Error, info: React.ErrorInfo) {
        console.error(`[editor] ${this.props.what} could not be shown:`, error, info.componentStack);
    }

    render() {
        const { failed } = this.state;
        if (!failed) return this.props.children;
        const { what, compact, action } = this.props;
        const raw = failed.message || String(failed);
        return (
            <div
                role="alert"
                data-editor-boundary=""
                title={raw}
                className={cn(
                    'flex items-start gap-2 rounded-md border border-danger/25 bg-danger-subtle text-danger',
                    compact ? 'w-56 px-2.5 py-2 text-xs' : 'm-4 px-3 py-2.5 text-sm',
                )}
            >
                <AlertCircle className={cn('shrink-0', compact ? 'mt-px size-3.5' : 'mt-0.5 size-4')} aria-hidden />
                <div className="min-w-0 space-y-1.5">
                    <p className="leading-snug"><span className="font-medium">{what}</span> can&rsquo;t be shown: {saidOf(raw)}</p>
                    {!compact && <p className="leading-snug text-muted">The rest of the workflow is unchanged, and nothing was saved.</p>}
                    {action}
                </div>
            </div>
        );
    }
}
