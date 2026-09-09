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
import { useState } from "react";
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

    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
    const [draftAgent, setDraftAgent] = useState<any>(null);
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

    const confirmDeleteAgent = async () => {
        if (!confirmDelete) return;
        try {
            await fetch(`/api/agents/${confirmDelete}`, { method: "DELETE" });
            dispatch(removeAgent(confirmDelete));
            // Clear the editor if it was showing the agent that just went away.
            if (selectedAgentId === confirmDelete) {
                setSelectedAgentId(null);
                setDraftAgent(null);
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
                availableCapabilities={capabilities}
                loadingCapabilities={loadingCapabilities}
                customTools={customTools}
                onDeleteAgent={(id: string) => setConfirmDelete(id)}
                providers={models.providers || {}}
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

            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </>
    );
}
