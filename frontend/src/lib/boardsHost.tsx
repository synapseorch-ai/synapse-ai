'use client';

/*
 * OSS's answers to the Boards screens' questions (`components/boards/host.tsx`, BOARD1b).
 *
 * The routes are `/api/boards…`, the principals are the workspace's agents, workflows and schedules, and
 * everyone may change anything (OSS has no roles). Cloud keeps its own copy of this file; the screens are shared.
 */
import type { ReactNode } from 'react';

import { BoardsHostProvider, type BoardsHost, type Principal } from '@/components/boards/host';

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`/api/boards${path}`, {
        method,
        headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 204) return undefined as T;
    const data = await res.json().catch(() => null);
    if (!res.ok) {
        const detail = data?.detail;
        const message = typeof detail === 'string' ? detail
            : Array.isArray(detail) ? detail.map((d: { msg?: string }) => d?.msg).filter(Boolean).join(' ')
                : `The request failed (${res.status}).`;
        throw Object.assign(new Error(message), { status: res.status });
    }
    return data as T;
}

type Row = { id?: unknown; name?: unknown; system?: unknown };

async function list(url: string): Promise<Row[]> {
    try {
        const data = await (await fetch(url)).json();
        return Array.isArray(data) ? data : [];
    } catch {
        return [];
    }
}

async function loadPrincipals(): Promise<Principal[]> {
    const [agents, workflows, schedules] = await Promise.all([
        list('/api/agents'), list('/api/orchestrations'), list('/api/schedules'),
    ]);
    const named = (kind: Principal['kind'], rows: Row[]) => rows
        .filter(r => r?.id && !r?.system)
        .map(r => ({ kind, id: String(r.id), name: String(r.name || r.id) }));
    return [...named('agent', agents), ...named('workflow', workflows), ...named('schedule', schedules)];
}

export const ossBoardsHost: BoardsHost = {
    request: request as BoardsHost['request'],
    loadPrincipals,
    canEdit: true,
    linkTo: (kind, id) => (kind === 'workflow' ? `/workflows?id=${encodeURIComponent(id)}` : '/schedules'),
    boardHref: id => `/boards?board=${encodeURIComponent(id)}`,
    offHint: 'Turn them on in Settings → General, then give agents their boards.',
};

export function OssBoardsHost({ children }: { children: ReactNode }) {
    return <BoardsHostProvider value={ossBoardsHost}>{children}</BoardsHostProvider>;
}
