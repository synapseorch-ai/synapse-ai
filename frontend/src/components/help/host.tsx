'use client';

/*
 * Help on every page (H1, D192): what each product tells the "?" panel.
 *
 * Everything in `components/help/` is copied byte for byte into the cloud app by
 * its `scripts/sync-ui.sh`, so nothing here may import a module the two products
 * shape differently (`@/lib/nav`, `@/types`, a store). What differs arrives
 * through the host each product's shell passes to `HelpProvider`: OSS's in
 * `lib/helpHost.tsx`, cloud's in its own `lib/helpHost.tsx`.
 *
 * - Where the docs live, and where their pictures live (`<mediaBase>/<id>.webp`,
 *   and `<id>.webm` beside it for a clip — HC0's layout on the docs site).
 * - Each page's topic: what the page is for, three to five steps, one picture,
 *   and the guide that goes further.
 * - Which topic a path is — each product's routes are its own.
 *
 * The pictures are never bundled and never listed at run time: the docs site
 * sends no CORS headers, so the manifest cannot be fetched from here, while a
 * plain `<img>` or `<video>` needs none. A topic carries its picture's id and
 * alt text itself, which is also what keeps the text whole when offline.
 */
import { createContext, useContext } from 'react';

/** One picture or clip on the docs site. */
export type HelpMedia = {
    /** `<area>/<name>`, HC0's path under the docs site's `/media`. */
    id: string;
    /** A looping clip exists beside the still, which is then its poster. */
    clip?: boolean;
    /** What it shows, for anyone who cannot see it. */
    alt: string;
};

/** What the panel says about one page. */
export type HelpTopic = {
    /** The panel's heading: the page's name. */
    title: string;
    /** What the page is for, in a sentence or two. */
    summary: string;
    /** Three to five things to do here, in order. */
    steps: string[];
    media?: HelpMedia;
    /** A docs path (`/guides/…`), joined to `docsBase`. */
    guide: string;
};

/**
 * One stop of a page's guided tour (GUIDE1, D318, F919): the part it lights up, named by its `data-tour` anchor,
 * and what that part is for. Naveen: "a highlighter will highlight a section and explain what we can do with this
 * ... and they just click next".
 */
export type TourStep = {
    /** The `data-tour` value on the element (a kit prop may set it; `test_every_tour_step_has_its_anchor`). */
    anchor: string;
    title: string;
    body: string;
};

/** A page's tour. Raising `version` shows it once more to people who saw the old one. */
export type Tour = { version: number; steps: TourStep[] };

export type HelpHost = {
    /** The docs site, with no trailing slash. */
    docsBase: string;
    /** Where its pictures live, with no trailing slash. */
    mediaBase: string;
    topics: Record<string, HelpTopic>;
    /** The topic for a path; `null` when none fits, and the panel shows `fallback`. */
    topicFor: (pathname: string) => string | null;
    /** A topic id that always exists: what the panel says on a page it has no topic for. */
    fallback: string;
    /** Guided tours, keyed by topic id: each runs from its page's "?" panel ("Take the tour"). */
    tours?: Record<string, Tour>;
    /**
     * The one topic whose tour starts by itself, once, on its first visit: Home's (F1457; Naveen 2026-11-08: a tour
     * on every new page was too many). Every other tour waits for "Take the tour"; with none named, none starts.
     */
    firstRunTour?: string;
    /**
     * The tabs of the section a page sits in, one stop each saying what it is for (GUIDE1, F1028; Naveen: "when they
     * land on runs page let's show other tabs and its uses"). `id` is the section's, so it shows once per section,
     * whichever of its tabs is visited first; after a page's own tour when both are new.
     */
    tabTourFor?: (topicId: string) => { id: string; tour: Tour } | undefined;
};

export const DOCS_BASE = 'https://docs.synapseorch.com';

export const DEFAULT_HELP_HOST: HelpHost = {
    docsBase: DOCS_BASE,
    mediaBase: `${DOCS_BASE}/media`,
    topics: {},
    topicFor: () => null,
    fallback: '',
};

export type HelpState = {
    host: HelpHost;
    open: boolean;
    setOpen: (open: boolean) => void;
    /** A part a screen names while it shows (`useHelpTopic`), over the path's topic; returns its release. */
    holdTopic: (id: string, at: number) => () => void;
    /** The topic the panel shows now, and its id. */
    topicId: string;
    topic: HelpTopic | undefined;
    /** This page's tour, when it has one, and a way to run it now (the panel's "Take the tour"). */
    tour: Tour | undefined;
    startTour: () => void;
};

/** `null` outside a `HelpProvider`, which is how the "?" knows not to draw itself. */
export const HelpContext = createContext<HelpState | null>(null);

export function useHelp(): HelpState | null {
    return useContext(HelpContext);
}
