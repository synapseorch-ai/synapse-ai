"use client";

/*
 * Whether local (stdio) MCP servers may run, and which commands they may be.
 *
 * A stdio server is a local command this backend starts, which is why
 * GHSA-3j67 tells anyone whose install is reachable over a network to turn it
 * off. Until S11 nobody could: neither setting was accepted by the settings
 * route, which answered 200 and dropped them. They live here, beside the
 * servers they govern, rather than in General.
 */
import { useEffect, useState } from "react";
import { Terminal } from "lucide-react";

import { Button, Input, Label, Switch } from "@/components/ui";

type Status = { tone: "ok" | "error"; text: string } | null;

/** "npx, uvx /usr/bin/node" → ["npx", "uvx", "node"]: basenames, as enforced. */
function parseCommands(raw: string): string[] {
    const names = raw.split(/[\s,]+/).map(s => s.trim()).filter(Boolean).map(s => s.split("/").pop() || s);
    return Array.from(new Set(names));
}

export function StdioPolicy() {
    const [loaded, setLoaded] = useState(false);
    const [allowed, setAllowed] = useState(true);
    const [scaleMode, setScaleMode] = useState(false);
    const [commands, setCommands] = useState("");
    const [savedCommands, setSavedCommands] = useState("");
    const [saving, setSaving] = useState(false);
    const [status, setStatus] = useState<Status>(null);

    useEffect(() => {
        fetch("/api/settings")
            .then(r => (r.ok ? r.json() : Promise.reject()))
            .then(s => {
                setAllowed(s.allow_stdio_mcp !== false);
                setScaleMode(!!s.scale_mode_enabled);
                const list = Array.isArray(s.mcp_command_allowlist) ? s.mcp_command_allowlist.join(", ") : "";
                setCommands(list);
                setSavedCommands(list);
                setLoaded(true);
            })
            .catch(() => setStatus({ tone: "error", text: "Could not read the local server settings." }));
    }, []);

    const save = async (patch: Record<string, unknown>, done: string) => {
        setSaving(true);
        setStatus(null);
        try {
            const res = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(patch),
            });
            if (!res.ok) throw new Error();
            setStatus({ tone: "ok", text: done });
            return true;
        } catch {
            setStatus({ tone: "error", text: "Not saved. Try again." });
            return false;
        } finally {
            setSaving(false);
        }
    };

    const toggle = async () => {
        const next = !allowed;
        if (await save({ allow_stdio_mcp: next }, next
            ? "Local servers are allowed."
            : "Local servers are off. Ones already running stop at the next restart or reconnect.")) {
            setAllowed(next);
        }
    };

    const saveCommands = async () => {
        const list = parseCommands(commands);
        if (await save({ mcp_command_allowlist: list }, list.length
            ? `Only ${list.join(", ")} may run as local servers.`
            : "Any command may run as a local server.")) {
            setCommands(list.join(", "));
            setSavedCommands(list.join(", "));
        }
    };

    return (
        <section aria-labelledby="stdio-policy-heading" className="space-y-4 rounded-xl border border-border bg-surface p-4 sm:p-6">
            <div className="flex items-start justify-between gap-4">
                <div>
                    <h2 id="stdio-policy-heading" className="text-section text-text flex items-center gap-2">
                        <Terminal className="h-4 w-4 text-text-faint" aria-hidden /> Local (stdio) servers
                    </h2>
                    <p className="text-sm text-text-muted mt-1 max-w-prose">
                        An app “on this machine” is a program Synapse starts on the computer it runs on. If other
                        people can reach this install over a network, turn these off.
                    </p>
                    {scaleMode && (
                        <p className="text-xs text-text mt-1">Scale mode is on, which keeps local servers off whatever this says.</p>
                    )}
                </div>
                <Switch
                    checked={allowed}
                    onChange={() => { void toggle(); }}
                    label="Allow local (stdio) servers"
                    disabled={!loaded || saving}
                />
            </div>

            <div className="space-y-2">
                <Label htmlFor="stdio-allowed-commands" size="sm" className="block">Allowed commands</Label>
                <div className="flex gap-2">
                    <Input
                        id="stdio-allowed-commands"
                        value={commands}
                        onChange={e => setCommands(e.target.value)}
                        placeholder="Any command (e.g. npx, uvx)"
                        disabled={!loaded}
                        className="font-mono"
                    />
                    <Button
                        variant="secondary"
                        onClick={saveCommands}
                        disabled={!loaded || saving || commands === savedCommands}
                    >
                        Save
                    </Button>
                </div>
                <p className="text-xs text-text-faint">Command names, comma-separated. Empty allows any command on the path.</p>
            </div>

            {status && (
                <p role="status" className={`text-xs ${status.tone === "ok" ? "text-success" : "text-danger"}`}>{status.text}</p>
            )}
        </section>
    );
}
