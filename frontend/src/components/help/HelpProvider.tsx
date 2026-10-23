'use client';

/*
 * The "?" panel's state, mounted once by each product's shell.
 *
 * It also fills the kit's page-header slot with the "?", so every page built on
 * `Screen` has one without asking; the few headers that are not (OSS's chat, the
 * cloud workflow editor's toolbar) render `<HelpButton />` themselves.
 *
 * The panel opens on a click and only then — never on a first visit, never from
 * a key, and nothing is remembered about having seen it (Naveen: "when
 * clicking"). One panel for the whole app, rendered here, so two "?" on one
 * screen could never open two.
 *
 * A page's guided tour (GUIDE1, F919) is the one thing shown without a click: once per browser on a page's first
 * visit, after the page settles, never over a dialog or the open panel, and never again once finished or put away.
 * The panel's "Take the tour" runs it any time.
 */
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { PageHeaderSlotProvider } from '@/components/ui';

import { HelpButton } from './HelpButton';
import { HelpPanel } from './HelpPanel';
import { HelpContext, useHelp, type HelpHost, type HelpState } from './host';
import { markTourSeen, screenBusy, tourSeen } from './tour/tour';

/** How long a page settles before its first-visit tour starts. */
const TOUR_AFTER_MS = 1500;

export function HelpProvider({ host, children }: { host: HelpHost; children: ReactNode }) {
    const pathname = usePathname() ?? '/';
    const [open, setOpen] = useState(false);
    const [override, setOverride] = useState<string | null>(null);

    const topicId = (override && host.topics[override] ? override : null)
        ?? host.topicFor(pathname)
        ?? host.fallback;
    const tour = host.tours?.[topicId];
    const touring = useRef(false);
    const startTour = useCallback(() => {
        if (!tour || touring.current) return;
        touring.current = true;
        setOpen(false);
        void import('./tour/runTour').then(({ runTour }) => runTour(tour.steps, () => {
            touring.current = false;
            markTourSeen(topicId, tour);
        }));
    }, [tour, topicId]);

    useEffect(() => {
        if (!tour || open || tourSeen(topicId, tour)) return;
        const timer = setTimeout(() => {
            if (!screenBusy()) startTour();
        }, TOUR_AFTER_MS);
        return () => clearTimeout(timer);
    }, [tour, topicId, open, startTour]);

    const value = useMemo<HelpState>(
        () => ({
            host, open, setOpen, setOverride, topicId, topic: host.topics[topicId] ?? host.topics[host.fallback],
            tour, startTour,
        }),
        [host, open, topicId, tour, startTour],
    );

    return (
        <HelpContext.Provider value={value}>
            <PageHeaderSlotProvider slot={<HelpButton />}>{children}</PageHeaderSlotProvider>
            <HelpPanel />
        </HelpContext.Provider>
    );
}

/**
 * Name the part of a page that is showing, for as long as it shows: the workflow
 * editor over the list, an agent being edited, a knowledge base that is open.
 * The page's own path decides the topic again when this unmounts or gets `null`.
 */
export function useHelpTopic(id: string | null) {
    const setOverride = useHelp()?.setOverride;
    useEffect(() => {
        if (!setOverride || !id) return;
        setOverride(id);
        return () => setOverride(current => (current === id ? null : current));
    }, [setOverride, id]);
}
