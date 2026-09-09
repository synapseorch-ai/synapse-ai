"use client";

/*
 * The four lists every settings screen shares, hydrated once.
 *
 * `fetchAllSettingsData` pulls agents, MCP servers, custom tools and models in
 * one round of four requests, and the slice is keyed by a single `initialized`
 * flag rather than per-list loading state. Before the split, `SettingsView`
 * owned that dispatch on behalf of all twenty tabs; now three screens mount
 * independently and each has to be able to be the first one loaded — a deep
 * link to /tools has no /agents render before it to have done the fetching.
 *
 * Guarded on `initialized`, so moving between the three costs nothing.
 */
import { useEffect } from "react";
import { useDispatch, useSelector } from "react-redux";

import { RESOURCE_SAVED_EVENT } from "@/components/app/BuilderProvider";
import type { AppDispatch, RootState } from "@/store";
import { fetchAllSettingsData } from "@/store/settingsSlice";

export function useSettingsData() {
    const dispatch = useDispatch<AppDispatch>();
    const settings = useSelector((state: RootState) => state.settings);

    useEffect(() => {
        if (!settings.initialized) dispatch(fetchAllSettingsData());
    }, [settings.initialized, dispatch]);

    // The guard above means these lists are fetched once and never again, which
    // was fine while the only way to change them was a form on the same screen.
    // The AI builder writes agents, tools and MCP servers from anywhere, so a
    // save has to invalidate them.
    useEffect(() => {
        const refetch = () => { dispatch(fetchAllSettingsData()); };
        window.addEventListener(RESOURCE_SAVED_EVENT, refetch);
        return () => window.removeEventListener(RESOURCE_SAVED_EVENT, refetch);
    }, [dispatch]);

    return settings;
}
