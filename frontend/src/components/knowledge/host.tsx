'use client';

/*
 * What the Knowledge screens need from the product they run in (D175).
 *
 * Everything in `components/knowledge/` is copied byte for byte into the cloud
 * app by its `scripts/sync-ui.sh`, so nothing here may import a module the two
 * products shape differently (`@/lib/models`, `@/types`, a store). What does
 * differ arrives through this context, which each product's page provides:
 * OSS's in `lib/knowledgeHost.tsx`, cloud's in its own `lib/knowledgeHost.tsx`.
 *
 * - The workspace's models: which settings name the defaults, how a model list
 *   is grouped and labelled, where the rerank keys live.
 * - The rerank models the workspace's keys can use (BF9), and which setting each
 *   provider's models need.
 * - Whether web sources and Evaluate are served yet.
 * - Whether this person may change anything (cloud's viewers may not; OSS has
 *   no roles) — a control that could only be refused is not drawn.
 * - How a part URL whose signature ran out is signed again (cloud's expire).
 *
 * Without a provider the screens get `DEFAULT_KNOWLEDGE_HOST`: every surface on,
 * no models to pick from. Provide a module-level value, not one built while
 * rendering: the screens read the workspace again whenever the host changes.
 */
import { createContext, useContext, type ReactNode } from 'react';

import type { ComboboxOption } from '@/components/ui';
import { RERANK_ROUTES, type RefreshParts, type RerankRoute } from '@/lib/knowledge';

export type { RerankRoute };

/** The workspace as the Knowledge screens read it. */
export type KnowledgeWorkspace = {
    /** What a base naming no embedding model embeds with; '' when nothing is set. */
    embedding: string;
    /** Embedding models the workspace can reach, as picker options. */
    embeddingModels: ComboboxOption[];
    /** Chat models the workspace can reach: Evaluate's writer, answerer and judge. */
    chatModels: ComboboxOption[];
    /** The workspace's chat model, where Evaluate's pickers start. */
    model: string;
    /** The workspace's judge model; '' when none is set. */
    judge: string;
    /** What a base naming no Contextual Retrieval model writes contexts with: the deployment's default, else the
     *  workspace's chat model ('' when neither). A host that leaves it out shows the workspace's chat model. */
    contextual?: string;
    /** What a base naming no rerank model reranks with; '' when nothing is set. A host that leaves it out shows
     *  none. */
    rerank?: string;
    /** The rerank models the workspace's keys can use, under their providers (BF9): never a name a key cannot call. */
    rerankModels: ComboboxOption[];
    /** Whether each `RerankRoute.key` is set. */
    keys: Record<string, boolean>;
};

export type KnowledgeHost = {
    /** The workspace's models, read and shaped as this product does. Never rejects. */
    loadWorkspace: () => Promise<KnowledgeWorkspace>;
    /** Keep a set model visible when no option matches it (its provider has gone), marked as such. */
    withUnreachable: (options: ComboboxOption[], value: string | undefined) => ComboboxOption[];
    /** Who serves a rerank model, by its id's prefix, and the setting it needs: a saved model's missing key. */
    rerankRoutes: readonly RerankRoute[];
    /** Web pages as sources (S1): "Add web pages" and the Web sources list. */
    webSources: boolean;
    /** Evaluate (V1): a base's question sets and runs. */
    evaluate: boolean;
    /** May change bases, documents, uploads and question sets; reading and searching need nothing. */
    canEdit: boolean;
    /** Scanned pages and images are read by OCR here (cloud: Textract). Without it a self-hosted install reports them
     *  rather than reading them, and the drop zone says so. */
    ocr: boolean;
    /** Fresh part URLs for an upload, where part URLs expire. */
    refreshParts?: RefreshParts;
    /** How a person turns Knowledge on here, for the screen shown while it is off; absent where nobody in the
     *  workspace can (cloud's switch is the deployment's). */
    offHint?: string;
};

export const EMPTY_WORKSPACE: KnowledgeWorkspace = {
    embedding: '', embeddingModels: [], chatModels: [], model: '', judge: '', rerankModels: [], keys: {},
};

export const DEFAULT_KNOWLEDGE_HOST: KnowledgeHost = {
    loadWorkspace: async () => EMPTY_WORKSPACE,
    withUnreachable: (options, value) =>
        !value || options.some(o => o.value === value) ? options : [...options, { value, label: value, group: 'Not reachable' }],
    rerankRoutes: RERANK_ROUTES,
    webSources: true,
    evaluate: true,
    canEdit: true,
    ocr: false,
};

const KnowledgeHostContext = createContext<KnowledgeHost>(DEFAULT_KNOWLEDGE_HOST);

export function KnowledgeHostProvider({ value, children }: { value: KnowledgeHost; children: ReactNode }) {
    return <KnowledgeHostContext.Provider value={value}>{children}</KnowledgeHostContext.Provider>;
}

export function useKnowledgeHost(): KnowledgeHost {
    return useContext(KnowledgeHostContext);
}
