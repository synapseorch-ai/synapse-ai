"use client";

/*
 * /agents — its own screen, not a branch of SettingsView.
 *
 * AgentsTab took 13 props from a component whose other job was holding forty
 * provider API-key fields. What it actually needs is three lists (agents, the
 * tools they can be given, the capabilities catalogue), the default model an
 * agent falls back to, and a selection.
 *
 * The builder agent is filtered out here rather than in AgentsTab. It is a real
 * row in the same store — the thing that *builds* agents is itself an agent —
 * but it is not one a user edits or deletes, and every consumer of this list
 * has wanted it hidden.
 */
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { useDispatch } from "react-redux";

import { ConfirmDialog, Toast, useToast } from "@/components/ui";
import type { AppDispatch } from "@/store";
import { removeAgent } from "@/store/settingsSlice";

import { AgentsTab } from "./AgentsTab";
import { useAppSettings } from "./hooks/useAppSettings";
import { useCapabilities } from "./hooks/useCapabilities";
import { useSettingsData } from "./hooks/useSettingsData";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** The native builder agent is infrastructure, not one of the user's agents. */

export function AgentsScreen() {
    const dispatch = useDispatch<AppDispatch>();
    const { agents, customTools, models, loading } = useSettingsData();
    const { capabilities, loading: loadingCapabilities } = useCapabilities();
    const { defaultModel } = useAppSettings();
    const { toast, show, dismiss } = useToast();

    const router = useRouter();
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
    const [draftAgent, setDraftAgent] = useState<any>(null);
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

    // What the draft was when it was last opened or saved. The draft used to be
    // replaced wholesale by whichever agent was clicked next, and nothing held
    // anyone on the screen, so an unsaved edit simply vanished (F73).
    const [baseline, setBaseline] = useState("null");
    const loadDraftAgent = useCallback((agent: any) => {
        setDraftAgent(agent);
        setBaseline(JSON.stringify(agent));
    }, []);
    const dirty = draftAgent !== null && JSON.stringify(draftAgent) !== baseline;

    // One question, whichever way out: another agent, a new one, or a link.
    const [pending, setPending] = useState<null | (() => void)>(null);
    const guard = useCallback((go: () => void) => {
        if (dirty) setPending(() => go);
        else go();
    }, [dirty]);

    // Leaving. A reload or a closed tab gets the browser's own prompt; a link
    // inside the app gets ours, because the App Router has no way to stop a
    // navigation once it has started — so the click is taken before it does.
    useEffect(() => {
        if (!dirty) return;
        const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); };
        const onClick = (e: MouseEvent) => {
            if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            const link = (e.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null;
            if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
            const url = new URL(link.href, window.location.href);
            if (url.origin !== window.location.origin || url.pathname === window.location.pathname) return;
            e.preventDefault();
            e.stopPropagation();
            setPending(() => () => router.push(url.pathname + url.search + url.hash));
        };
        window.addEventListener("beforeunload", onBeforeUnload);
        document.addEventListener("click", onClick, true);
        return () => {
            window.removeEventListener("beforeunload", onBeforeUnload);
            document.removeEventListener("click", onClick, true);
        };
    }, [dirty, router]);

    const confirmDeleteAgent = async () => {
        if (!confirmDelete) return;
        try {
            await fetch(`/api/agents/${confirmDelete}`, { method: "DELETE" });
            dispatch(removeAgent(confirmDelete));
            // Clear the editor if it was showing the agent that just went away.
            if (selectedAgentId === confirmDelete) {
                setSelectedAgentId(null);
                loadDraftAgent(null);
            }
        } catch {
            show("Could not delete the agent", "danger");
        } finally {
            setConfirmDelete(null);
        }
    };

    return (
        <>
            <AgentsTab
                agents={agents}
                selectedAgentId={selectedAgentId}
                setSelectedAgentId={setSelectedAgentId}
                draftAgent={draftAgent}
                setDraftAgent={setDraftAgent}
                loadDraftAgent={loadDraftAgent}
                guard={guard}
                availableCapabilities={capabilities}
                loadingCapabilities={loadingCapabilities}
                customTools={customTools}
                onDeleteAgent={(id: string) => setConfirmDelete(id)}
                providers={models.providers || {}}
                reasoningCapable={models.reasoning_capable || []}
                reasoningWithoutOff={models.reasoning_without_off || []}
                defaultModel={defaultModel}
                loadingAgents={loading}
            />

            <ConfirmDialog
                open={!!confirmDelete}
                onClose={() => setConfirmDelete(null)}
                onConfirm={confirmDeleteAgent}
                title="Delete this agent?"
                description="Its prompt, tools and model settings go with it. This cannot be undone."
            />

            <ConfirmDialog
                open={pending !== null}
                onClose={() => setPending(null)}
                onConfirm={() => {
                    const go = pending;
                    setPending(null);
                    go?.();
                }}
                title="Discard unsaved changes?"
                description={<>Your changes to <strong>{draftAgent?.name || "this agent"}</strong> have not been saved.</>}
                confirmLabel="Discard changes"
            />

            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </>
    );
}
