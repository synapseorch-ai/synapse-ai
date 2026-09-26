'use client';
/**
 * The way into the AI builder, from anywhere.
 *
 * Bottom-right, above the toast host — which is why the toasts moved up: two
 * things cannot own the same corner, and the toast is the one that appears
 * unbidden.
 */

import { useBuilder } from '@/components/app/BuilderProvider';
import { Hint, Mark } from '@/components/ui';
import { cn, focusRing } from '@/lib/cn';

export function BuilderLauncher() {
    const { isOpen, open, streaming, state } = useBuilder();
    // A question waiting on the person, whether asked in this page or brought
    // back by a reload (F214): with the panel closed, this is the only sign.
    const waiting = state.pending !== null;

    if (isOpen) return null;

    return (
        // The wrapper takes the fixed position, not the button. `Hint` wraps
        // its child in an inline span — deliberately, so a tooltip still fires
        // on a disabled button — and around a fixed button that span stayed
        // behind in the page flow. After the `h-screen` shell it landed as one
        // 24px line of body text, so every page in the app scrolled onto a
        // blank strip; and the tooltip anchored to the span, opening in the
        // bottom-left corner half below the window to describe a button in the
        // bottom-right. `scripts/ui-smoke.py`'s `layout` check fails if
        // anything in the page flow wraps something fixed again.
        <div className="fixed bottom-5 right-5 z-40">
            <Hint content="Build with AI" side="left">
                <button
                    type="button"
                    onClick={() => open()}
                    aria-label={waiting ? 'Open the AI builder (waiting for your answer)' : 'Open the AI builder'}
                    className={cn(
                        // `relative`, because the fixed position that used to
                        // anchor the streaming dot now lives on the wrapper.
                        'relative flex h-11 w-11 items-center justify-center',
                        'rounded-full border border-border-strong bg-surface text-accent shadow-xl',
                        'transition-colors hover:bg-row-hover hover:text-accent-hover',
                        focusRing,
                    )}
                >
                    <Mark size={22} compact={false} aria-hidden />
                    {(streaming || waiting) && (
                        <span
                            className={cn(
                                'absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full',
                                waiting ? 'bg-warning' : 'bg-accent',
                            )}
                            aria-hidden
                        />
                    )}
                </button>
            </Hint>
        </div>
    );
}
