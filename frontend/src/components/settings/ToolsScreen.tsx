"use client";

/*
 * /tools — its own screen, not a branch of SettingsView.
 *
 * CustomToolsTab took 17 props, the widest of the three, and most of them were
 * one editor's draft state passed down through a component that also owned the
 * Models tab. All of it is local here.
 *
 * The two save paths stay separate on purpose. A Python tool is code plus a
 * schema; an HTTP/n8n tool is a URL, headers and two JSON schemas that are
 * edited as *text* and have to parse before anything is sent. Merging them
 * would mean one validate step that understands both, which is how the JSON
 * errors ended up being reported after the request rather than before it.
 */
import { useEffect, useState } from "react";
import { useDispatch } from "react-redux";

import { ConfirmDialog, Toast, useToast } from "@/components/ui";
import type { AppDispatch } from "@/store";
import { removeCustomTool, updateCustomTool } from "@/store/settingsSlice";

import { CustomToolsTab } from "./CustomToolsTab";
import { useAppSettings } from "./hooks/useAppSettings";
import { useSettingsData } from "./hooks/useSettingsData";

/* eslint-disable @typescript-eslint/no-explicit-any */

export function ToolsScreen() {
    const dispatch = useDispatch<AppDispatch>();
    const { customTools } = useSettingsData();
    const { n8nBaseUrl, n8nIntegrated } = useAppSettings();
    const { toast, show, dismiss } = useToast();

    const [draftTool, setDraftTool] = useState<any>(null);
    const [toolBuilderMode, setToolBuilderMode] = useState<"config" | "python">("config");
    const [headerRows, setHeaderRows] = useState<{ id: string; key: string; value: string }[]>([]);
    const [n8nWorkflowId, setN8nWorkflowId] = useState<string | null>(null);
    const [n8nWorkflows, setN8nWorkflows] = useState<any[]>([]);
    const [n8nWorkflowsLoading, setN8nWorkflowsLoading] = useState(false);
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

    /*
     * The workflow list is only worth fetching once the builder is open on the
     * config tab — it is a dropdown inside a form most visits never open, and
     * n8n may not be running at all.
     */
    useEffect(() => {
        if (!draftTool || toolBuilderMode !== "config") return;
        if (n8nWorkflows.length > 0 || n8nWorkflowsLoading) return;

        let live = true;
        setN8nWorkflowsLoading(true);
        fetch("/api/n8n/workflows")
            .then((res) => (res.ok ? res.json() : []))
            .then((data) => {
                if (live) setN8nWorkflows(Array.isArray(data) ? data : []);
            })
            .catch(() => {
                if (live) setN8nWorkflows([]);
            })
            .finally(() => {
                if (live) setN8nWorkflowsLoading(false);
            });
        return () => {
            live = false;
        };
    }, [draftTool, toolBuilderMode, n8nWorkflows.length, n8nWorkflowsLoading]);

    const persist = async (payload: unknown, successMessage: string) => {
        try {
            const res = await fetch("/api/tools/custom", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                show("Failed to save tool", "danger");
                return;
            }
            const savedResp = await res.json();
            dispatch(updateCustomTool(savedResp?.tool ?? savedResp));
            setDraftTool(null);
            setToolBuilderMode("config");
            show(successMessage);
        } catch {
            show("Error saving tool", "danger");
        }
    };

    const handleSaveTool = async () => {
        if (!draftTool) return;

        if (draftTool.tool_type === "python") {
            if (!draftTool.name) return show("System Name is required", "warning");
            if (!draftTool.code?.trim()) return show("Python code cannot be empty", "warning");

            return persist(
                {
                    name: draftTool.name,
                    generalName: draftTool.generalName || draftTool.name,
                    description: draftTool.description || "",
                    tool_type: "python",
                    code: draftTool.code,
                    inputSchema: draftTool.inputSchema || { type: "object", properties: {} },
                    schemaParams: draftTool.schemaParams || [],
                    // Picked field by field, so a field left off this list is
                    // erased on every save — which is what happened to a tool's
                    // declared globals, set anywhere but here (F28).
                    globals: draftTool.globals || [],
                },
                "Python tool saved successfully",
            );
        }

        if (!draftTool.name || !draftTool.url) return show("Name and URL are required", "warning");

        // Both schemas are edited as text, so they are parsed before anything is
        // sent — a save that reaches the API and fails there loses the draft.
        let inputSchema = draftTool.inputSchema;
        let outputSchema = draftTool.outputSchema;

        try {
            if (typeof draftTool.inputSchemaStr === "string") {
                inputSchema = JSON.parse(draftTool.inputSchemaStr);
            }
        } catch {
            return show("Invalid Input Schema JSON", "danger");
        }

        try {
            outputSchema = draftTool.outputSchemaStr?.trim()
                ? JSON.parse(draftTool.outputSchemaStr)
                : undefined;
        } catch {
            return show("Invalid Output Schema JSON", "danger");
        }

        const headers: Record<string, string> = {};
        for (const row of headerRows) {
            if (row.key.trim()) headers[row.key.trim()] = row.value;
        }

        const payload = { ...draftTool, inputSchema, outputSchema, headers };
        delete payload.inputSchemaStr;
        delete payload.outputSchemaStr;

        return persist(payload, "Tool saved successfully");
    };

    const confirmDeleteTool = async () => {
        if (!confirmDelete) return;
        try {
            await fetch(`/api/tools/custom/${confirmDelete}`, { method: "DELETE" });
            dispatch(removeCustomTool(confirmDelete));
        } catch {
            show("Could not delete the tool", "danger");
        } finally {
            setConfirmDelete(null);
        }
    };

    return (
        <>
            <CustomToolsTab
                customTools={customTools}
                draftTool={draftTool}
                setDraftTool={setDraftTool}
                toolBuilderMode={toolBuilderMode}
                setToolBuilderMode={setToolBuilderMode}
                headerRows={headerRows}
                setHeaderRows={setHeaderRows}
                n8nWorkflows={n8nWorkflows}
                n8nWorkflowsLoading={n8nWorkflowsLoading}
                n8nWorkflowId={n8nWorkflowId}
                setN8nWorkflowId={setN8nWorkflowId}
                getN8nBaseUrl={() => n8nBaseUrl}
                onSaveTool={handleSaveTool}
                onDeleteTool={(name) => setConfirmDelete(name)}
                // Already persisted by the backend when the spec was uploaded;
                // this only folds them into the store.
                onImported={(tools: any[]) => {
                    tools.forEach((t) => dispatch(updateCustomTool(t)));
                    show(`Imported ${tools.length} tool${tools.length === 1 ? "" : "s"}`);
                }}
                n8nIntegrated={n8nIntegrated}
            />

            <ConfirmDialog
                open={!!confirmDelete}
                onClose={() => setConfirmDelete(null)}
                onConfirm={confirmDeleteTool}
                title="Delete custom tool?"
                description={
                    <>
                        <span className="font-mono">{confirmDelete}</span> will stop being offered
                        to agents. Agents that reference it keep the name in their tool list.
                    </>
                }
            />

            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </>
    );
}
