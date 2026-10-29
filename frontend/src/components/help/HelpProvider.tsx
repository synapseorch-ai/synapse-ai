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
 * A dialog up when it is due makes it wait for that dialog to close; a first-run surface holds it back from the start
 * (`tour/hold`, F1060), so onboarding and the model step always come first and the tour once after.
 * The panel's "Take the tour" runs it any time.
 */
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import { PageHeaderSlotProvider } from '@/components/ui';

import { HelpButton } from './HelpButton';
import { HelpPanel } from './HelpPanel';
import { HelpContext, useHelp, type HelpHost, type HelpState } from './host';
import { tourMayStart, useToursHeld } from './tour/hold';
import { markTourSeen, screenBusy, tourSeen } from './tour/tour';

/** How long a page settles before its first-visit tour starts. */
const TOUR_AFTER_MS = 1500;

export function HelpProvider({ host, children }: { host: HelpHost; children: ReactNode }) {
    const pathname = usePathname() ?? '/';
    const [open, setOpen] = useState(false);
    // The parts a screen names while they show (`useHelpTopic`), each by when it first rendered: a part inside
    // another renders after it, so the newest is the innermost, and a part that closes leaves the rest (F1191).
    const [shownParts, setShownParts] = useState<{ id: string; at: number }[]>([]);
    const holdTopic = useCallback((id: string, at: number) => {
        setShownParts(list => [...list.filter(p => p.at !== at), { id, at }]);
        return () => setShownParts(list => list.filter(p => p.at !== at));
    }, []);
    const partTopic = shownParts.filter(p => host.topics[p.id]).sort((a, b) => b.at - a.at)[0]?.id ?? null;

    const topicId = partTopic
        ?? host.topicFor(pathname)
        ?? host.fallback;
    // A page's own tour, and its section's tabs (F1028): each remembered on its own, run as one when both are new.
    const pageTour = host.tours?.[topicId];
    // Memoised: a fresh object each render would restart the first-visit timer forever.
    const tabs = useMemo(() => host.tabTourFor?.(topicId), [host, topicId]);
    const parts = useMemo(
        () => [
            ...(pageTour ? [{ id: topicId, tour: pageTour }] : []),
            ...(tabs ? [tabs] : []),
        ],
        [pageTour, tabs, topicId],
    );
    const tour = useMemo(
        () => (parts.length ? { version: 1, steps: parts.flatMap(p => p.tour.steps) } : undefined),
        [parts],
    );
    const touring = useRef(false);
    const run = useCallback((which: typeof parts) => {
        if (!which.length || touring.current) return;
        touring.current = true;
        setOpen(false);
        void import('./tour/runTour').then(({ runTour }) => runTour(which.flatMap(p => p.tour.steps), () => {
            touring.current = false;
            for (const p of which) markTourSeen(p.id, p.tour);
        }));
    }, []);
    const startTour = useCallback(() => run(parts), [run, parts]);

    // A first-run surface (onboarding, the model step) holds the tour back while it is up (F1060).
    const held = useToursHeld();
    useEffect(() => {
        const unseen = parts.filter(p => !tourSeen(p.id, p.tour));
        if (!unseen.length || !tourMayStart({ held, panelOpen: open })) return;
        let timer: ReturnType<typeof setTimeout> | undefined;
        let watch: MutationObserver | null = null;
        const arm = () => {
            timer = setTimeout(() => {
                if (!screenBusy()) return run(unseen);
                // A dialog has the screen: the tour waits for it to close, then lets the page settle again.
                watch = new MutationObserver(() => {
                    if (screenBusy()) return;
                    watch?.disconnect();
                    watch = null;
                    arm();
                });
                watch.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['role'] });
            }, TOUR_AFTER_MS);
        };
        arm();
        return () => {
            clearTimeout(timer);
            watch?.disconnect();
        };
    }, [parts, open, held, run]);

    const value = useMemo<HelpState>(
        () => ({
            host, open, setOpen, holdTopic, topicId, topic: host.topics[topicId] ?? host.topics[host.fallback],
            tour, startTour,
        }),
        [host, open, holdTopic, topicId, tour, startTour],
    );

    return (
        <HelpContext.Provider value={value}>
            <PageHeaderSlotProvider slot={<HelpButton />}>{children}</PageHeaderSlotProvider>
            <HelpPanel />
        </HelpContext.Provider>
    );
}

/** How many `useHelpTopic` parts have rendered: each part's place in the order. */
let rendered = 0;

/**
 * Name the part of a page that is showing, for as long as it shows: the workflow
 * editor over the list, an agent being edited, a knowledge base that is open.
 * When it unmounts or gets `null`, the part it sits in, or else the page's own path, decides again.
 */
export function useHelpTopic(id: string | null) {
    const holdTopic = useHelp()?.holdTopic;
    // Taken at first render, parent before child: effects run child first, so their order would put the editor
    // over a Browser step that mounted with it.
    const [at] = useState(() => ++rendered);
    useEffect(() => {
        if (!holdTopic || !id) return;
        return holdTopic(id, at);
    }, [holdTopic, id, at]);
}
