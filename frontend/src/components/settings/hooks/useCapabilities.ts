"use client";

/*
 * The tool catalogue an agent picks from, grouped the way the picker shows it.
 *
 * `/api/tools/available` returns a flat list where each entry knows its
 * `source` — `gmail`, `filesystem`, an MCP server name — and its `type`. The
 * picker wants one row per source with its tools nested, EXCEPT for legacy
 * `custom_http` tools, which are ungrouped: each is its own capability, because
 * a user who wrote one tool thinks of it as one thing, not as a member of a
 * "custom_http" family.
 *
 * Lifted out of SettingsView unchanged. Only AgentsTab consumes it, but it
 * cannot live inside AgentsTab: the picker is rendered from two places and the
 * fetch would run twice.
 */
import { useEffect, useState } from "react";
import type { ToolAnnotations } from "@/lib/toolHints";
import type { SizedTool } from "@/lib/toolTokens";

/** One tool in a capability. `annotations`: what it says it does (TAG1); `trusted`: false for an outside server's
 *  or the AI builder's claim, which the tool policy believes only when it restricts. */
export type ToolDetail = { name: string; description: string; tokens?: number; annotations?: ToolAnnotations; trusted?: boolean };

export type Capability = {
    id: string;
    label: string;
    description?: string;
    tools: string[];
    /** `tokens`: what the tool adds to every model call (F483, `lib/toolTokens.ts`). */
    toolDetails: ToolDetail[];
    /** Drives the badge: MCP, native, or a user's own tool. */
    toolType: "mcp" | "native" | "custom";
};

type AvailableTool = {
    name: string;
    source?: string;
    source_label?: string;
    description?: string;
    label?: string;
    type?: string;
    /** A custom tool's group id and name, when it belongs to one. */
    group?: string | null;
    group_label?: string | null;
    /** What it adds to every model call (F483): the engine's `declaration_tokens`. */
    tokens?: number;
    annotations?: ToolAnnotations;
    trusted?: boolean;
};

function detail(t: AvailableTool): ToolDetail {
    return { name: t.name, description: t.description || "", tokens: t.tokens, annotations: t.annotations, trusted: t.trusted };
}

/** The tool an agent sends helpers with; stored in its `tools` like any other (D204). */
export const HELPERS_TOOL = "spawn_subtask";

function group(tools: AvailableTool[]): Capability[] {
    const groups: Record<string, Capability> = {};

    for (const t of tools) {
        // Helpers have a switch of their own ("Can send helpers", SUB1a), never a row here:
        // a row would also let unticking another tool on an "every tool" agent switch them on.
        if (t.name === HELPERS_TOOL) continue;
        if (t.source === "custom_http" && t.group) {
            // A grouped tool: one capability per group, taken whole. The agent's
            // list then holds `group:<id>`, which the engine expands at run
            // time — a tool added to the group later is included automatically.
            const key = `group:${t.group}`;
            groups[key] ??= {
                id: key,
                label: t.group_label || t.group,
                description: `Every tool in ${t.group_label || t.group}`,
                tools: [key],
                toolDetails: [],
                toolType: "custom",
            };
            groups[key].toolDetails.push(detail(t));
            continue;
        }
        if (t.source === "custom_http") {
            // One capability per tool, not one per source. Deduped by name in
            // case the same tool is reported twice.
            groups[t.name] ??= {
                id: t.name,
                label: t.label || t.name,
                description: t.description,
                tools: [t.name],
                toolDetails: [detail(t)],
                toolType: "custom",
            };
            continue;
        }

        const source = t.source || "unknown";
        groups[source] ??= {
            id: source,
            label:
                t.source_label ||
                source.charAt(0).toUpperCase() + source.slice(1).replace(/_/g, " "),
            description: `Tools from ${t.source_label || source}`,
            tools: [],
            toolDetails: [],
            toolType:
                t.type === "mcp_external" ? "mcp" : t.type === "mcp_native" ? "native" : "custom",
        };
        groups[source].tools.push(t.name);
        groups[source].toolDetails.push(detail(t));
    }

    return Object.values(groups);
}

/** The tools the editor's context line counts (F483): connected servers' and the workspace's own. */
export function sizedTools(caps: Capability[]): SizedTool[] {
    return caps.flatMap((cap) => cap.toolType === "native" ? [] : cap.toolDetails.map((t) => ({
        name: t.name,
        tokens: t.tokens ?? 0,
        server: cap.toolType === "mcp" ? cap.id : undefined,
    })));
}

/** An agent's tool list with each whole group (`group:<id>`) it holds expanded to the group's tools. */
export function selectedToolNames(caps: Capability[], tools: string[]): Set<string> {
    const out = new Set(tools);
    for (const cap of caps) {
        if (cap.id.startsWith("group:") && out.has(cap.id)) cap.toolDetails.forEach((t) => out.add(t.name));
    }
    return out;
}

export function useCapabilities() {
    const [capabilities, setCapabilities] = useState<Capability[]>([]);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let live = true;
        fetch("/api/tools/available")
            .then((res) => res.json())
            .then((data) => {
                if (live) setCapabilities(group(data.tools || []));
            })
            .catch(() => {
                if (live) setCapabilities([]);
            })
            .finally(() => {
                if (live) setLoading(false);
            });
        return () => {
            live = false;
        };
    }, []);

    return { capabilities, loading };
}
