'use client';

/**
 * Whether the workspace's everyday model can answer, and the first-run model
 * step that opens when it cannot (ONB2, F728, D303).
 *
 * Read once when the app loads, from `GET /api/onboarding/models` — the one
 * readiness the backend gives (`core/model_readiness.py`): no key saved, no
 * address, Ollama not answering or without the model, a model CLI not on this
 * computer. The step opens on its own only until it is closed once, by any of
 * its ways (`first_run_models_closed`); Home's "Connect a model" and Chat's line
 * open it again with `open()`. An installer answer that gave a working model
 * never sees it.
 */
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';

import { ModelSetup } from './ModelSetup';

type Readiness = { model: string; ready: boolean; problem: string | null; closed: boolean };

const ModelSetupContext = createContext<{
    /** null until read. */
    ready: boolean | null;
    model: string | null;
    open: () => void;
    /** Moves each time the models may have changed, for a screen that shows them. */
    version: number;
}>({ ready: null, model: null, open: () => {}, version: 0 });

export const useModelSetup = () => useContext(ModelSetupContext);

async function readiness(): Promise<Readiness | null> {
    try {
        const res = await fetch('/api/onboarding/models');
        return res.ok ? await res.json() : null;
    } catch {
        return null;
    }
}

export function ModelSetupProvider({ children }: { children: ReactNode }) {
    const [state, setState] = useState<Readiness | null>(null);
    const [open, setOpen] = useState(false);
    const [version, setVersion] = useState(0);

    useEffect(() => {
        let cancelled = false;
        void readiness().then(r => {
            if (cancelled || !r) return;
            setState(r);
            if (!r.ready && !r.closed) setOpen(true);
        });
        return () => { cancelled = true; };
    }, []);

    const reread = useCallback(async () => {
        const r = await readiness();
        if (r) setState(r);
        setVersion(v => v + 1);
    }, []);

    // Closed by any of its ways: it does not open on its own again.
    const close = useCallback(() => {
        setOpen(false);
        void fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ first_run_models_closed: true }),
        }).catch(() => {}).finally(() => void reread());
    }, [reread]);

    const saved = useCallback(() => {
        setOpen(false);
        void reread();
    }, [reread]);

    return (
        <ModelSetupContext.Provider value={{ ready: state?.ready ?? null, model: state?.model ?? null, open: () => setOpen(true), version }}>
            {children}
            <ModelSetup open={open} problem={state?.ready === false ? state.problem : null} onClose={close} onSaved={saved} />
        </ModelSetupContext.Provider>
    );
}
