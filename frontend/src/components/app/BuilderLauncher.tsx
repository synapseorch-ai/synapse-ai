'use client';
/**
 * The way into the AI builder, from anywhere.
 *
 * Bottom-right, above the toast host — which is why the toasts moved up: two
 * things cannot own the same corner, and the toast is the one that appears
 * unbidden.
 */
import { Sparkles } from 'lucide-react';

import { useBuilder } from '@/components/app/BuilderProvider';
import { Hint } from '@/components/ui';
import { cn, focusRing } from '@/lib/cn';

export function BuilderLauncher() {
    const { isOpen, open, streaming } = useBuilder();

    if (isOpen) return null;

    return (
        <Hint content="Build with AI" side="left">
            <button
                type="button"
                onClick={() => open()}
                aria-label="Open the AI builder"
                className={cn(
                    'fixed bottom-5 right-5 z-40 flex h-11 w-11 items-center justify-center',
                    'rounded-full border border-border-strong bg-surface text-accent shadow-xl',
                    'transition-colors hover:bg-row-hover hover:text-accent-hover',
                    focusRing,
                )}
            >
                <Sparkles size={17} aria-hidden />
                {streaming && (
                    <span
                        className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-accent"
                        aria-hidden
                    />
                )}
            </button>
        </Hint>
    );
}
