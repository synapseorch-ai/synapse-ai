'use client';

/*
 * What the run screens need from the product they run in (M2b).
 *
 * `components/run/` is copied byte for byte into the cloud app, so nothing in it
 * may know a URL, a fetch helper or a store. What differs arrives through this
 * context, which each product's page provides from a module-level value: OSS's
 * in `lib/runHost.tsx`, cloud's in its own `lib/runHost.tsx`.
 *
 * - Where things are: the run list, a run, the editor, the settings a fix needs.
 * - How a run's events are read: OSS replays its journal and follows it live on
 *   one stream; cloud follows a live run on its stream and reads a finished
 *   one's archive.
 * - What a person can do to a run: answer, cancel, replay. Each resolves with
 *   the sentence to show, or throws an Error whose message says why not.
 *
 * Without a provider the screens get `DEFAULT_RUN_HOST`: nothing to read and
 * nothing to do, so a missing provider shows empty, never a broken request.
 */
import { createContext, useContext, type ReactNode } from 'react';

import type { HumanValues } from '../orchestration/humanFields';
import type { NestedRun } from './model';

export type RunLinks = {
    /** The run list. */
    runs: string;
    /** One run's page. */
    run: (runId: string) => string;
    /** The workflow, opened where the run can be seen on it (OSS restores the run on the canvas). */
    editor: (run: { id: string; workflowId: string }) => string;
    /** Where model keys live; absent where nobody here can set one. */
    models?: string;
    /** The agents. */
    agents?: string;
    /** Where Knowledge is switched on, and its bases. */
    knowledge?: string;
    /** Where the model wallet is topped up (cloud). */
    billing?: string;
};

export type RunHost = {
    links: RunLinks;
    /**
     * Every event of a run, oldest first, calling `onEvent` for each. Live runs
     * are followed until they end or `signal` aborts; resolves when nothing more
     * is coming. Rejects with an Error whose message can be shown.
     */
    events: (runId: string, live: boolean, onEvent: (event: unknown) => void, signal: AbortSignal) => Promise<void>;
    /** The events exactly as the engine emitted them, for the raw log. */
    rawEvents: (runId: string) => Promise<unknown[]>;
    /** The runs this one's steps started (F251). */
    children: (runId: string) => Promise<NestedRun[]>;
    /** Answer a waiting run: a mapping of the step's fields, or the text. Resolves with what to say. */
    answer: (runId: string, response: string | HumanValues | Record<string, unknown>) => Promise<string>;
    /** Stop a run. Resolves with what to say ("Run cancelled.", "Stopping…"). */
    cancel: (runId: string) => Promise<string>;
    /** Replay a failed or cancelled run from where it stopped. Resolves with what to say. */
    replay: (runId: string) => Promise<string>;
};

const nothing = async () => { throw new Error('This page has no run host.'); };

export const DEFAULT_RUN_HOST: RunHost = {
    links: { runs: '/runs', run: (id) => `/runs/${encodeURIComponent(id)}`, editor: () => '/' },
    events: async () => {},
    rawEvents: async () => [],
    children: async () => [],
    answer: nothing,
    cancel: nothing,
    replay: nothing,
};

const RunHostContext = createContext<RunHost>(DEFAULT_RUN_HOST);

export function RunHostProvider({ value, children }: { value: RunHost; children: ReactNode }) {
    return <RunHostContext.Provider value={value}>{children}</RunHostContext.Provider>;
}

export function useRunHost(): RunHost {
    return useContext(RunHostContext);
}
