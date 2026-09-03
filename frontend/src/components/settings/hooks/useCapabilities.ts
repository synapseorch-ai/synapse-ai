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

export type Capability = {
    id: string;
    label: string;
    description?: string;
    tools: string[];
    toolDetails: { name: string; description: string }[];
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
};

function group(tools: AvailableTool[]): Capability[] {
    const groups: Record<string, Capability> = {};

    for (const t of tools) {
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
            groups[key].toolDetails.push({ name: t.name, description: t.description || "" });
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
                toolDetails: [{ name: t.name, description: t.description || "" }],
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
        groups[source].toolDetails.push({ name: t.name, description: t.description || "" });
    }

    return Object.values(groups);
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
