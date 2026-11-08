'use client';

/*
 * What a Vault tree needs to know about Knowledge (D157), when it is on: which
 * folders are knowledge bases, where each of their files stands, and which
 * folders Knowledge fills itself. Both products' Vaults draw their own trees;
 * this is the part they share (D175). Absent, a tree is the plain Vault it
 * always was.
 */
import { useEffect, useState } from 'react';

import { Badge } from '@/components/ui';
import { REASONS, STATUS, WORKING, knowledgeApi, type KnowledgeBase, type KnowledgeDocument } from '@/lib/knowledge';

export interface VaultKnowledge {
    /** The base whose folder this is. */
    baseAt: (path: string) => KnowledgeBase | null;
    /** The switched-on base this path is in, or is. */
    baseHolding: (path: string) => KnowledgeBase | null;
    /** A folder Knowledge fills itself — a base's fetched pages (`_web`) or records (`_records`): no
     *  uploads or new files there (the backend refuses them, and the next crawl would overwrite them). */
    managed: (path: string) => boolean;
    docAt: (path: string) => KnowledgeDocument | null;
    onUpload: (folderPath: string, kb: KnowledgeBase) => void;
}

/** The bases and every document of each, keyed by path; read again on `reloadKey`, and every 3 s while any
 *  document is being worked on. Nothing is read while `active` is false. */
export function useVaultKnowledge(active: boolean, reloadKey: number) {
    const [bases, setBases] = useState<KnowledgeBase[]>([]);
    const [docs, setDocs] = useState<Record<string, KnowledgeDocument>>({});
    const [tick, setTick] = useState(0);

    useEffect(() => {
        if (!active) return;
        let live = true;
        knowledgeApi.bases()
            .then(async list => {
                const all = await Promise.all(list.map(kb => knowledgeApi.documents(kb.id).catch(() => [])));
                if (!live) return;
                setBases(list);
                setDocs(Object.fromEntries(all.flat().map(d => [d.path, d])));
            })
            .catch(() => { });
        return () => { live = false; };
    }, [active, reloadKey, tick]);

    const working = active && Object.values(docs).some(d => WORKING.has(d.status));
    useEffect(() => {
        if (!working) return;
        const timer = setInterval(() => setTick(t => t + 1), 3000);
        return () => clearInterval(timer);
    }, [working]);

    return {
        bases: active ? bases : [],
        docs: active ? docs : {},
        reload: () => setTick(t => t + 1),
    };
}

/** The tree's lookups over what `useVaultKnowledge` read; undefined while there is no base. */
export function vaultKnowledge(
    { bases, docs }: { bases: KnowledgeBase[]; docs: Record<string, KnowledgeDocument> },
    onUpload: VaultKnowledge['onUpload'],
): VaultKnowledge | undefined {
    if (!bases.length) return undefined;
    return {
        baseAt: path => bases.find(b => b.folder === path) ?? null,
        baseHolding: path => bases.find(b => b.status === 'active'
            && (path === b.folder || path.startsWith(`${b.folder}/`))) ?? null,
        managed: path => bases.some(b => ['_web', '_records'].some(dir => {
            const root = `${b.folder}/${dir}`;
            return path === root || path.startsWith(`${root}/`);
        })),
        docAt: path => docs[path] ?? null,
        onUpload,
    };
}

/** A file's Knowledge status, beside its name — never inside the name's button, whose name tests find it by. */
export function DocBadge({ doc }: { doc: KnowledgeDocument }) {
    const s = STATUS[doc.status];
    return (
        <span title={doc.status_reason ? `${s.label}: ${REASONS[doc.status_reason] ?? doc.status_reason}` : s.label}
            className="shrink-0">
            <Badge size="sm" tone={s.tone}>{s.label}</Badge>
        </span>
    );
}
