'use client';

import { CircleHelp } from 'lucide-react';

import { IconButton } from '@/components/ui';

import { useHelp } from './host';

/**
 * The "?" — opens the panel about the page it sits on.
 *
 * Draws nothing outside a `HelpProvider` (sign-in, an embedded widget), so a
 * shared header can carry it unconditionally.
 */
export function HelpButton({ className }: { className?: string }) {
    const help = useHelp();
    if (!help) return null;
    return (
        <IconButton
            icon={CircleHelp}
            label="Help for this page"
            aria-haspopup="dialog"
            aria-expanded={help.open}
            onClick={() => help.setOpen(true)}
            className={className}
            data-tour="help"
        />
    );
}
