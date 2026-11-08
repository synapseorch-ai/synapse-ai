'use client';
/*
 * Which flagged pages to offer: Knowledge, Boards, Channels, Repos and DB Configs show
 * only while their /api/settings switch is on.
 *
 * Read once for the shell and shared by everything that lists pages — the
 * rail, Settings' column and a hub's tabs — instead of each fetching its own
 * copy, which left them disagreeing until the slower one landed. Read again
 * when Settings turns login or Knowledge on or off.
 */
import { createContext, useContext, useEffect, useState } from 'react';

import { BOARDS_SWITCHED_EVENT } from '@/components/boards/board';
import { AUTH_CHANGED_EVENT } from '@/lib/auth';
import { KNOWLEDGE_SWITCHED_EVENT } from '@/lib/knowledge';
import type { NavFlag } from '@/lib/nav';

export type NavFlags = Partial<Record<NavFlag, boolean>>;

const NavFlagsContext = createContext<NavFlags>({});

export function NavFlagsProvider({ children }: { children: React.ReactNode }) {
    const [flags, setFlags] = useState<NavFlags>({});
    useEffect(() => {
        let live = true;
        const read = () => fetch('/api/settings')
            .then(r => r.json())
            .then(data => {
                if (live) {
                    setFlags({
                        messaging_enabled: !!data?.messaging_enabled,
                        coding_agent_enabled: !!data?.coding_agent_enabled,
                        knowledge_enabled: !!data?.knowledge_enabled,
                        boards_enabled: !!data?.boards_enabled,
                    });
                }
            })
            .catch(() => { });
        read();
        window.addEventListener(AUTH_CHANGED_EVENT, read);
        window.addEventListener(KNOWLEDGE_SWITCHED_EVENT, read);
        window.addEventListener(BOARDS_SWITCHED_EVENT, read);
        return () => {
            live = false;
            window.removeEventListener(AUTH_CHANGED_EVENT, read);
            window.removeEventListener(KNOWLEDGE_SWITCHED_EVENT, read);
            window.removeEventListener(BOARDS_SWITCHED_EVENT, read);
        };
    }, []);
    return <NavFlagsContext.Provider value={flags}>{children}</NavFlagsContext.Provider>;
}

export function useNavFlags(): NavFlags {
    return useContext(NavFlagsContext);
}
