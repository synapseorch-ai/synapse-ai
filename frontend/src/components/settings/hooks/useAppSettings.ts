"use client";

/*
 * The slice of /api/settings a screen needs to read but never writes.
 *
 * `SettingsView` fetches the whole settings payload into ~50 useState pairs
 * because the General and Models tabs *edit* it. The three screens split out of
 * it only ever read four fields — the default model an agent falls back to, the
 * reasoning effort it runs at without one of its own, and the n8n URL and key
 * that decide whether the tool builder can offer a workflow picker — so they
 * get the payload read-only rather than 50 setters drilled through them.
 *
 * Deliberately not in Redux. This is request state, not shared app state: the
 * settings screens own the writes, and a cache here would go stale the moment
 * one of them saved.
 */
import { useEffect, useState } from "react";

export type AppSettings = {
    model?: string;
    n8n_url?: string;
    n8n_api_key?: string;
    /** What an agent with no effort of its own runs at (F99). */
    reasoning_effort?: string;
};

export function useAppSettings() {
    const [settings, setSettings] = useState<AppSettings>({});

    useEffect(() => {
        let live = true;
        fetch("/api/settings")
            .then((res) => res.json())
            .then((data) => {
                if (live) setSettings(data || {});
            })
            .catch(() => {
                /* the screens all have a sane default without it */
            });
        return () => {
            live = false;
        };
    }, []);

    /** Trailing slashes are stripped so callers can concatenate a path. */
    const n8nBaseUrl = (settings.n8n_url || "http://localhost:5678").replace(/\/+$/, "");

    return {
        settings,
        n8nBaseUrl,
        n8nIntegrated: !!settings.n8n_api_key?.trim(),
        defaultModel: settings.model || "",
    };
}
