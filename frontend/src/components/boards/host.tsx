'use client';

/*
 * What the Boards screens need from the product they run in (BOARD1b, D282).
 *
 * Everything in `components/boards/` is copied byte for byte into the cloud app by its `scripts/sync-ui.sh`, so
 * nothing here may know an address, a fetch helper or a store. What differs arrives through this context, which
 * each product's page provides: OSS's in `lib/boardsHost.tsx`, cloud's in its own `lib/boardsHost.tsx`.
 *
 * - `request`: the boards routes, under `/api/boards` (OSS) or `/bff/boards` (cloud).
 * - `loadPrincipals`: the agents, workflows and schedules an access list can name.
 * - `canEdit`: whether this person may change anything (cloud's viewers and approvers read; OSS has no roles). A
 *   control that could only be refused is not drawn.
 * - `linkTo`: where a workflow's or a schedule's board opens in place; `boardHref`: a board's page in the Boards tab.
 * - `offHint`: how a person turns Boards on here, for the screen shown while they are off.
 *
 * Without a provider the screens get `DEFAULT_BOARDS_HOST`, which reads nothing. Provide a module-level value,
 * not one built while rendering.
 */
import { createContext, useContext, type ReactNode } from 'react';

import type { BoardsRequest, PrincipalKind } from './board';

export type Principal = { kind: PrincipalKind; id: string; name: string };

export type BoardsHost = {
    request: BoardsRequest;
    /** Every agent, workflow and schedule an access list may name. Never rejects. */
    loadPrincipals: () => Promise<Principal[]>;
    canEdit: boolean;
    /** A workflow's or a schedule's page, where its board opens in place; absent where there is none. */
    linkTo?: (kind: 'workflow' | 'schedule', id: string) => string | null;
    /** A board's page in the Boards tab. */
    boardHref?: (boardId: string) => string;
    offHint?: string;
};

export const DEFAULT_BOARDS_HOST: BoardsHost = {
    request: async () => { throw new Error('Boards are not available here.'); },
    loadPrincipals: async () => [],
    canEdit: false,
};

const BoardsHostContext = createContext<BoardsHost>(DEFAULT_BOARDS_HOST);

export function BoardsHostProvider({ value, children }: { value: BoardsHost; children: ReactNode }) {
    return <BoardsHostContext.Provider value={value}>{children}</BoardsHostContext.Provider>;
}

export function useBoardsHost(): BoardsHost {
    return useContext(BoardsHostContext);
}
