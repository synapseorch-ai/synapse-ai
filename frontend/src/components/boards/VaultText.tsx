'use client';
/**
 * A card's text with each `@[path]` it names as a link to that Vault file (F1204, Naveen 10-29): an agent writes its
 * numbers as a CSV in the Vault and its note names it, so the person opens the file, and its Chart view, from the
 * board. The rest stays the text it is.
 */
import Link from 'next/link';
import type { ReactNode } from 'react';
import { useBoardsHost } from './host';

/** `@[path]`, as the Vault's own mentions are written (`core.vault.mention_key`): a path with no bracket in it. */
const MENTION = /@\[([^\[\]\n]+)\]/g;

export function VaultText({ text }: { text: string }) {
    const host = useBoardsHost();
    const href = host.vaultHref ?? ((path: string) => `/vault?path=${encodeURIComponent(path)}`);
    const parts: ReactNode[] = [];
    let at = 0;
    for (const m of text.matchAll(MENTION)) {
        const start = m.index ?? 0;
        if (start > at) parts.push(text.slice(at, start));
        const path = m[1].trim();
        parts.push(
            <Link key={`${start}-${path}`} href={href(path)} className="font-code text-accent underline-offset-2 hover:underline">
                {path}
            </Link>,
        );
        at = start + m[0].length;
    }
    if (at < text.length) parts.push(text.slice(at));
    return <>{parts}</>;
}
