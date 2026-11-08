"use client";
/*
 * Chat used to own the "an agent is still processing" confirm because the
 * Settings gear lived in its own header. The gear now lives on the rail, which
 * knows nothing about the chat page's in-flight SSE stream — so the chat page
 * registers a guard here and the rail asks before it navigates.
 *
 * Without this, clicking any rail item mid-run aborts the stream silently.
 */
import { createContext, useCallback, useContext, useEffect, useRef } from 'react';

/** Return false to cancel the navigation. */
type Guard = () => boolean;

const NavGuardContext = createContext<{
    register: (guard: Guard | null) => void;
    mayLeave: () => boolean;
} | null>(null);

export function NavGuardProvider({ children }: { children: React.ReactNode }) {
    const guardRef = useRef<Guard | null>(null);

    const register = useCallback((guard: Guard | null) => {
        guardRef.current = guard;
    }, []);

    const mayLeave = useCallback(() => guardRef.current?.() ?? true, []);

    return (
        <NavGuardContext.Provider value={{ register, mayLeave }}>
            {children}
        </NavGuardContext.Provider>
    );
}

/** Rail side: call before pushing a route. */
export function useNavGuard() {
    return useContext(NavGuardContext)?.mayLeave ?? (() => true);
}

/**
 * Page side: hold navigation while `guard` returns false. Kept in a ref by the
 * provider, so re-registering on every render is cheap and always current.
 */
export function useRegisterNavGuard(guard: Guard) {
    const ctx = useContext(NavGuardContext);
    useEffect(() => {
        ctx?.register(guard);
        return () => ctx?.register(null);
    });
}
