'use client';

import { useSyncExternalStore } from 'react';
import { CircleHelp } from 'lucide-react';

import { IconButton } from '@/components/ui';

import { useHelp } from './host';

/**
 * The "?" — opens the panel about the page it sits on.
 *
 * Draws nothing outside a `HelpProvider` (sign-in, an embedded widget), so a
 * shared header can carry it unconditionally.
 *
 * `data-ready` appears once it is hydrated and a press would open the panel:
 * the browser checks wait for it rather than press a server-drawn "?" that
 * does nothing yet.
 */
const noSubscribe = () => () => {};

export function HelpButton({ className }: { className?: string }) {
    const help = useHelp();
    const hydrated = useSyncExternalStore(noSubscribe, () => true, () => false);
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
            data-ready={hydrated || undefined}
        />
    );
}
