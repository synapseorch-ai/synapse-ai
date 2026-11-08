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
 * - Whether this person may (F452): `canAnswer` and `canControl`. OSS has one
 *   login, so both are true; cloud fills them from the member's role, so a
 *   viewer is never offered a button the API answers with a 403.
 *
 * Without a provider the screens get `DEFAULT_RUN_HOST`: nothing to read and
 * nothing to do, so a missing provider shows empty, never a broken request.
 */
import { createContext, useContext, type ReactNode } from 'react';

import type { BrowserHost } from '../browser/model';
import type { HumanValues } from '../orchestration/humanFields';
import type { NestedRun } from './model';

export type RunLinks = {
    /** The run list. */
    runs: string;
    /** One run's page. */
    run: (runId: string) => string;
    /** The workflow, opened where the run can be seen on it (OSS restores the run on the canvas). */
    editor: (run: { id: string; workflowId: string }) => string;
    /** The workflow with its settings open, where its cap per run is (CAP1); absent, the editor. */
    workflowSettings?: (run: { id: string; workflowId: string }) => string;
    /** Where model keys live; absent where nobody here can set one. */
    models?: string;
    /** The agents. */
    agents?: string;
    /** Where Knowledge is switched on, and its bases. */
    knowledge?: string;
    /** Where the model wallet is topped up (cloud). */
    billing?: string;
    /** Where a Vault file opens (a step's output saved as a file, D339 P4); absent, `/vault?path=`. */
    vault?: (path: string) => string;
};

/**
 * A Team agent step's lines on the run page (TEAM1e, 20 Q5): a member's question answered in its line, by its own
 * id, and a member stopped or written to while the step works. Each resolves with what to say (an answer) or
 * whether it was read (Stop, Message); throws an Error whose message can be shown. Absent, the lines show with
 * nothing to press.
 */
export type RunTeamActions = {
    answer: (runId: string, questionId: string, approve: boolean, reason?: string) => Promise<string>;
    stop: (runId: string, memberId: string) => Promise<boolean>;
    message: (runId: string, memberId: string, text: string) => Promise<boolean>;
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
    /** Whether this person may answer a waiting run (cloud: an approver and up, AP1). */
    canAnswer: boolean;
    /** Whether this person may cancel or replay a run (cloud: a builder and up). */
    canControl: boolean;
    /** A Team step's lines: answered under `canAnswer`, stopped and written to under `canControl` (TEAM1e). */
    team?: RunTeamActions;
    /** The browser a step's agent worked in (BRW2): its screenshots, while it is open. Absent: lines without them. */
    browser?: BrowserHost;
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
    canAnswer: false,
    canControl: false,
};

const RunHostContext = createContext<RunHost>(DEFAULT_RUN_HOST);

export function RunHostProvider({ value, children }: { value: RunHost; children: ReactNode }) {
    return <RunHostContext.Provider value={value}>{children}</RunHostContext.Provider>;
}

export function useRunHost(): RunHost {
    return useContext(RunHostContext);
}
