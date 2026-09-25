"use client";

/*
 * /mcp-servers — its own screen, not a branch of SettingsView.
 *
 * McpServersTab took 12 props, every one of them drilled from the 1,087-line
 * component that also owned the Models tab's forty key fields. Its state was
 * never shared with anything else, so it lives here now: the drafts, the
 * connect/reconnect/delete flows, and the OAuth listener.
 *
 * The OAuth listener is why this is a component and not a hook. A remote MCP
 * server authorises in a popup, which posts back a MESSAGE to whoever opened
 * it, and the handler has to outlive the popup round trip. In SettingsView it
 * survived tab switches by virtue of the parent never unmounting; here the
 * screen itself is what stays mounted for the duration, which is the same
 * guarantee with a much smaller thing making it.
 */
import { useCallback, useEffect, useState } from "react";
import { useDispatch } from "react-redux";

import { ConfirmDialog, type ToastTone } from "@/components/ui";
import type { AppDispatch } from "@/store";
import { addMcpServer, removeMcpServer, updateMcpServerStatus } from "@/store/settingsSlice";

import { McpServersTab, type McpNotice } from "./McpServersTab";
import { StdioPolicy } from "./StdioPolicy";
import { useSettingsData } from "./hooks/useSettingsData";

type DraftServer = {
    name: string;
    label: string;
    server_type: "stdio" | "remote";
    command: string;
    args: string;
    env: { key: string; value: string }[];
    url: string;
    token: string;
};

const EMPTY_DRAFT: DraftServer = {
    name: "",
    label: "",
    server_type: "stdio",
    command: "",
    args: "",
    env: [],
    url: "",
    token: "",
};

export function McpServersScreen() {
    const dispatch = useDispatch<AppDispatch>();
    const { mcpServers, loading } = useSettingsData();

    /*
     * One notice, rendered inline above the server list rather than as the
     * kit's floating Toast. Half the messages here are instructions the user
     * has to act on — "complete OAuth in the popup", "use Retry to reconnect" —
     * and those must not fade out of the corner of the screen.
     */
    const [notice, setNotice] = useState<McpNotice | null>(null);
    const show = useCallback(
        (message: string, tone: ToastTone = "success") => setNotice({ message, tone }),
        [],
    );

    const [draft, setDraft] = useState<DraftServer>(EMPTY_DRAFT);
    const [isConnecting, setIsConnecting] = useState(false);
    const [lastConnected, setLastConnected] = useState<boolean | null>(null);
    const [pendingServerName, setPendingServerName] = useState<string | null>(null);
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

    /*
     * The popup posts back here when the user finishes authorising. Registered
     * once for the life of the screen — a listener added per render would miss
     * the message that arrives between two of them.
     */
    const handleOAuthMessage = useCallback(
        (event: MessageEvent) => {
            if (event.data?.type !== "MCP_OAUTH_COMPLETE") return;
            if (event.data.success) {
                const name = event.data.name as string;
                dispatch(updateMcpServerStatus({ name, status: "connected" }));
                show(`${name} connected via OAuth`);
                setPendingServerName(null);
            } else {
                show(`OAuth failed: ${event.data.error}`, "danger");
            }
        },
        [dispatch, show],
    );

    useEffect(() => {
        window.addEventListener("message", handleOAuthMessage);
        return () => window.removeEventListener("message", handleOAuthMessage);
    }, [handleOAuthMessage]);

    const handleAdd = async () => {
        if (!draft.name) return show("Server name is required.", "danger");
        if (draft.server_type === "stdio" && !draft.command)
            return show("Command is required for local servers.", "danger");
        if (draft.server_type === "remote" && !draft.url)
            return show("URL is required for remote servers.", "danger");

        // Quoted arguments survive the split, so a path with a space in it works.
        const argsList =
            draft.args.match(/(?:[^\s"]+|"[^"]*")+/g)?.map((s) => s.replace(/^"|"$/g, "")) || [];
        const envObj = draft.env.reduce<Record<string, string>>((acc, curr) => {
            if (curr.key) acc[curr.key] = curr.value;
            return acc;
        }, {});

        setIsConnecting(true);
        try {
            const res = await fetch("/api/mcp/servers", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: draft.name,
                    label: draft.label,
                    server_type: draft.server_type,
                    command: draft.command,
                    args: argsList,
                    env: envObj,
                    url: draft.url,
                    token: draft.token,
                }),
            });

            if (!res.ok) {
                const err = await res.json();
                show(`Error: ${err.detail || "Unknown error"}`, "danger");
                return;
            }

            const data = await res.json();
            dispatch(addMcpServer(data.config));
            const submittedName = draft.name;
            setDraft(EMPTY_DRAFT);

            if (data.status === "oauth_pending") {
                setLastConnected(false);
                setPendingServerName(submittedName);
                show("OAuth required — opening browser. Return here once authorised.", "warning");
                if (data.auth_url) window.open(data.auth_url, "_blank");
            } else if (data.connected) {
                setLastConnected(true);
                show("Server connected and saved");
            } else {
                // Saved but unreachable is neither success nor failure: the
                // config is persisted and Retry is the next step.
                setLastConnected(false);
                show("Config saved. Use Retry to reconnect.", "warning");
            }
        } catch {
            show("Failed to reach backend. Is it running?", "danger");
        } finally {
            setIsConnecting(false);
        }
    };

    const handleReconnect = async (name: string) => {
        try {
            dispatch(updateMcpServerStatus({ name, status: "connecting" }));
            const res = await fetch(`/api/mcp/servers/${name}/reconnect`, { method: "POST" });
            const data = await res.json();

            if (data.connected) {
                dispatch(updateMcpServerStatus({ name, status: "connected" }));
                show(`${name} reconnected`);
            } else if (data.needs_oauth && data.auth_url) {
                setPendingServerName(name);
                window.open(data.auth_url, "_blank");
                show(`Re-authenticating ${name} — complete OAuth in the popup.`, "warning");
            } else {
                dispatch(updateMcpServerStatus({ name, status: "disconnected" }));
                show(`Could not connect to ${name}. Try re-adding the server.`, "warning");
            }
        } catch {
            dispatch(updateMcpServerStatus({ name, status: "disconnected" }));
            show("Reconnect failed.", "danger");
        }
    };

    const confirmDeleteServer = async () => {
        if (!confirmDelete) return;
        try {
            const res = await fetch(`/api/mcp/servers/${confirmDelete}`, { method: "DELETE" });
            if (res.ok) dispatch(removeMcpServer(confirmDelete));
        } catch {
            show("Could not remove the server.", "danger");
        } finally {
            setConfirmDelete(null);
        }
    };

    return (
        <>
            <McpServersTab
                mcpServers={mcpServers}
                // Was hard-wired to `false` in SettingsView — `setLoadingMcp`
                // existed and was never called, so this spinner had never once
                // rendered. The store's own flag is what it always meant.
                loadingMcp={loading}
                isConnecting={isConnecting}
                lastConnected={lastConnected}
                pendingServerName={pendingServerName}
                onPendingResolved={() => setPendingServerName(null)}
                draftMcpServer={draft}
                setDraftMcpServer={setDraft}
                onAddServer={handleAdd}
                onDeleteServer={(name) => setConfirmDelete(name)}
                onReconnectServer={handleReconnect}
                notice={notice}
                onNotice={show}
            />

            <div className="mt-8">
                <StdioPolicy />
            </div>

            <ConfirmDialog
                open={!!confirmDelete}
                onClose={() => setConfirmDelete(null)}
                onConfirm={confirmDeleteServer}
                title="Remove MCP server?"
                description={
                    <>
                        <span className="font-mono">{confirmDelete}</span> will be disconnected and
                        its tools will stop being offered to agents.
                    </>
                }
                confirmLabel="Remove"
            />
        </>
    );
}
