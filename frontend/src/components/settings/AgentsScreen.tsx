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
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { useDispatch } from "react-redux";

import { ChevronDown, Plus, Sparkles } from "lucide-react";

import { AppScreen } from "@/components/app/AppScreen";
import { Button, ConfirmDialog, Menu, MenuItem, Toast, useToast } from "@/components/ui";
import type { AppDispatch } from "@/store";
import { removeAgent } from "@/store/settingsSlice";

import type { AgentStatus } from "./AgentTest";
import { AgentsTab } from "./AgentsTab";
import { useAppSettings } from "./hooks/useAppSettings";
import { useHelpTopic } from "@/components/help";
import { takeAgentDraft } from "@/lib/agentDraft";
import { useCapabilities } from "./hooks/useCapabilities";
import { useSettingsData } from "./hooks/useSettingsData";

/* eslint-disable @typescript-eslint/no-explicit-any */

/** Where the person's choice to show or hide the test chat is kept (M2d). */
const TEST_OPEN_KEY = "synapseAgentTestOpen";

export function AgentsScreen() {
    const dispatch = useDispatch<AppDispatch>();
    const { agents, customTools, models, loading } = useSettingsData();
    const { capabilities, loading: loadingCapabilities } = useCapabilities();
    const { defaultModel, settings } = useAppSettings();
    const { toast, show, dismiss } = useToast();

    const router = useRouter();
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
    const [draftAgent, setDraftAgent] = useState<any>(null);
    // An agent open in the editor has its own help and tour (GUIDE1, F1029), as the workflow editor has.
    useHelpTopic(draftAgent ? 'agents.editor' : null);
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

    // The agents workbench (M2d) never opens empty (UX1): with nothing asked
    // for, the first agent opens. After a delete, the next first one does.
    const params = useSearchParams();
    const askedFor = params.get("id");
    const askedForDraft = params.get("draft") === "1";
    useEffect(() => {
        if (draftAgent || askedFor || askedForDraft) return;
        const first = (agents as any[])[0];
        if (!first) return;
        setSelectedAgentId(first.id);
        loadDraftAgent({ ...first });
    }, [agents, draftAgent, askedFor, askedForDraft, loadDraftAgent]);

    // A new agent, unsaved until Save (it was the list's "+").
    const blankAgent = () => ({
        id: `agent_${Date.now()}`,
        name: "New Agent",
        description: "A custom agent.",
        system_prompt: "You are a helpful assistant.",
        tools: [] as string[],
        repos: [],
        type: "conversational",
        avatar: "default",
        max_turns: 30,
    });
    const newAgent = () => {
        const agent = blankAgent();
        guard(() => {
            loadDraftAgent(agent);
            setSelectedAgentId(agent.id);
        });
    };

    // The Save bar's Discard: back to what was opened or saved, or — for an
    // agent never saved — off it, onto the first agent. Asked first, always.
    const discard = () => {
        const saved = draftAgent && (agents as any[]).find((a) => a.id === draftAgent.id);
        setPending(() => () => {
            if (saved) loadDraftAgent(JSON.parse(baseline));
            else { setSelectedAgentId(null); loadDraftAgent(null); }
        });
    };

    // Whether each agent sees images, as chat reads it.
    const [agentStatus, setAgentStatus] = useState<Record<string, AgentStatus>>({});
    useEffect(() => {
        let live = true;
        fetch("/api/status")
            .then((r) => (r.ok ? r.json() : null))
            .then((d) => { if (live && d?.agents && typeof d.agents === "object") setAgentStatus(d.agents); })
            .catch(() => {});
        return () => { live = false; };
    }, [agents]);

    // The test chat's column: the person's last choice, else open on a screen
    // wide enough for three columns.
    const [aiBuilderOpen, setAiBuilderOpen] = useState(false);
    const [testOpen, setTestOpenState] = useState(true);
    useEffect(() => {
        let kept: string | null = null;
        try { kept = localStorage.getItem(TEST_OPEN_KEY); } catch { /* private window */ }
        setTestOpenState(kept === null ? window.innerWidth >= 1280 : kept === "1");
    }, []);
    const setTestOpen = (open: boolean) => {
        setTestOpenState(open);
        try { localStorage.setItem(TEST_OPEN_KEY, open ? "1" : "0"); } catch { /* private window */ }
    };

    // `?draft=1`: a helper's "Save as agent" (SUB1a, S9) — a new agent made from it, its brief as its
    // instructions, its tools and its model. Nothing is saved until Save: it reads as an unsaved edit of
    // a blank agent, so leaving asks.
    useEffect(() => {
        if (!askedForDraft) return;
        const url = new URL(window.location.href);
        url.searchParams.delete("draft");
        window.history.replaceState(null, "", `${url.pathname}${url.search}`);
        const draft = takeAgentDraft();
        if (!draft) return;
        const blank = blankAgent();
        const agent = { ...blank, name: draft.name.slice(0, 100) || blank.name, description: "Made from a helper.",
                        system_prompt: draft.system_prompt || blank.system_prompt, tools: draft.tools,
                        ...(draft.model ? { model: draft.model } : {}) };
        setSelectedAgentId(agent.id);
        setDraftAgent(agent);
        setBaseline(JSON.stringify(blank));
        // Once, on arrival: the draft is taken and the address no longer asks.
    }, [askedForDraft]);

    // `?id=<agent>` opens that agent — ⌘K's agents by name (M1a) — through the
    // same question as a click on another agent. The address then drops it, so
    // asking for the same agent again later is a new request.
    useEffect(() => {
        if (!askedFor) return;
        const agent = (agents as any[]).find((a) => a.id === askedFor);
        if (!agent) return;   // still loading, or no such agent
        const url = new URL(window.location.href);
        url.searchParams.delete("id");
        window.history.replaceState(null, "", `${url.pathname}${url.search}`);
        if (agent.id === selectedAgentId) return;
        guard(() => {
            setSelectedAgentId(agent.id);
            loadDraftAgent({ ...agent });
        });
    }, [askedFor, agents, selectedAgentId, guard, loadDraftAgent]);

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
            const res = await fetch(`/api/agents/${confirmDelete}`, { method: "DELETE" });
            // Refused while something uses it (F467): the server's sentence names
            // each, and the agent stays.
            if (!res.ok) {
                const err = await res.json().catch(() => null);
                show(typeof err?.detail === "string" ? err.detail : `Delete failed (HTTP ${res.status})`, "danger");
                return;
            }
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
        <AppScreen
            id="agents"
            bleed
            actions={(
                // New agent, and beside it the other way to make one: describe it
                // and let the AI write it (it was the list's sparkle; Naveen, M2d).
                <div className="flex items-center" data-tour="agents-new">
                    <Button size="sm" onClick={newAgent} aria-label="Create New Agent" title="Create New Agent" className="rounded-r-none">
                        <Plus className="size-4" aria-hidden /> New agent
                    </Button>
                    <Menu
                        align="end"
                        trigger={(
                            <Button size="sm" iconOnly aria-label="More ways to make an agent" title="More ways to make an agent" className="rounded-l-none border-l border-l-accent-fg/25">
                                <ChevronDown className="size-4" aria-hidden />
                            </Button>
                        )}
                    >
                        <MenuItem icon={Sparkles} onSelect={() => setAiBuilderOpen(true)}>Build Agent with AI…</MenuItem>
                    </Menu>
                </div>
            )}
        >
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
                reasoningOffMinimum={models.reasoning_off_minimum || []}
                defaultModel={defaultModel}
                quickModel={settings.builder_quick_model || ''}
                workspaceEffort={settings.reasoning_effort}
                loadingAgents={loading}
                dirty={dirty}
                onDiscard={discard}
                agentStatus={agentStatus}
                testOpen={testOpen}
                setTestOpen={setTestOpen}
                aiBuilderOpen={aiBuilderOpen}
                setAiBuilderOpen={setAiBuilderOpen}
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
        </AppScreen>
    );
}
