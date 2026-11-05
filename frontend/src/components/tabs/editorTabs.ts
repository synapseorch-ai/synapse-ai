/*
 * The workflow editor as one tab of the workflows page (TABS1 v2, F930/F1023):
 * the contract between the page's tabs and each product's editor (OSS's
 * OrchestrationTab, cloud's OrchestrationsClient).
 */

import type * as React from "react";

import type { CanvasViewport } from "../orchestration/CanvasWorkbench";
import type { DraftSnapshot } from "../orchestration/use-draft-history";

/**
 * The editor as one tab of the workflows page (TABS1 v2, F930). The page mounts
 * one editor, keyed by the tab in front, so each workflow's draft and run live
 * and end with its own tab: a tab going behind hands its draft back (`keep`),
 * stops reading its run (the run goes on) and comes forward from both. Whatever
 * would open another workflow here — the switcher, New, a row of the list, a
 * run — asks the page for a tab instead, so one editor never holds two
 * workflows' runs (F1026).
 */
export interface EditorTabs {
    /** The tab is a new workflow, opened blank under `initialOrchId`. */
    startNew?: boolean;
    /** The draft as the tab left it. */
    history?: DraftSnapshot;
    /** Where its canvas stood. */
    viewport?: CanvasViewport | null;
    open: (workflowId: string, runId?: string) => void;
    openNew: () => void;
    showList: () => void;
    /** `dirty`: the editor's own rule (an untouched blank workflow holds nothing to lose). */
    keep: (left: { history: DraftSnapshot; runId: string | null; dirty: boolean; viewport: CanvasViewport | null }) => void;
    report: (now: { title: string; dirty: boolean; running: boolean; waiting: boolean; workflowId: string | null }) => void;
    /** The workflow the tab names is gone (deleted here or elsewhere). */
    gone: () => void;
    /** A link the page opens as a tab: following it loses nothing, so it isn't asked about. */
    isTabLink: (url: URL) => boolean;
    /** Another tab holds unsaved work: leaving the page asks, as for this tab's own. */
    othersDirty?: boolean;
    /** The strip, drawn at the top of the editor and of the list. */
    strip: React.ReactNode;
    /** The page has read what its address asks for (a workflow, a run, a new one); until then the editor leaves the
     *  address as it is. The list wrote the bare `/workflows` over a link's `?id=` before the page read it, so a link
     *  from another page opened nothing (F1403). Absent: read. */
    addressRead?: boolean;
}

