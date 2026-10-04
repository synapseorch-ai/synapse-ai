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
 * - Which rerank models to suggest, and the key each needs.
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
import { RERANK_SUGGESTIONS, type RefreshParts } from '@/lib/knowledge';

/** A rerank model the settings suggest by name: who serves it, and the setting it needs (`null`: none). */
export type RerankChoice = { model: string; provider: string; key: string | null };

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
    /** Whether each `RerankChoice.key` is set. */
    keys: Record<string, boolean>;
};

export type KnowledgeHost = {
    /** The workspace's models, read and shaped as this product does. Never rejects. */
    loadWorkspace: () => Promise<KnowledgeWorkspace>;
    /** Keep a set model visible when no option matches it (its provider has gone), marked as such. */
    withUnreachable: (options: ComboboxOption[], value: string | undefined) => ComboboxOption[];
    /** Rerank models suggested by name; the first is the field's example. */
    rerankChoices: readonly RerankChoice[];
    /** Web pages as sources (S1): "Add web pages" and the Web sources list. */
    webSources: boolean;
    /** Evaluate (V1): a base's question sets and runs. */
    evaluate: boolean;
    /** May change bases, documents, uploads and question sets; reading and searching need nothing. */
    canEdit: boolean;
    /** Fresh part URLs for an upload, where part URLs expire. */
    refreshParts?: RefreshParts;
    /** How a person turns Knowledge on here, for the screen shown while it is off; absent where nobody in the
     *  workspace can (cloud's switch is the deployment's). */
    offHint?: string;
};

export const EMPTY_WORKSPACE: KnowledgeWorkspace = {
    embedding: '', embeddingModels: [], chatModels: [], model: '', judge: '', keys: {},
};

export const DEFAULT_KNOWLEDGE_HOST: KnowledgeHost = {
    loadWorkspace: async () => EMPTY_WORKSPACE,
    withUnreachable: (options, value) =>
        !value || options.some(o => o.value === value) ? options : [...options, { value, label: value, group: 'Not reachable' }],
    rerankChoices: RERANK_SUGGESTIONS,
    webSources: true,
    evaluate: true,
    canEdit: true,
};

const KnowledgeHostContext = createContext<KnowledgeHost>(DEFAULT_KNOWLEDGE_HOST);

export function KnowledgeHostProvider({ value, children }: { value: KnowledgeHost; children: ReactNode }) {
    return <KnowledgeHostContext.Provider value={value}>{children}</KnowledgeHostContext.Provider>;
}

export function useKnowledgeHost(): KnowledgeHost {
    return useContext(KnowledgeHostContext);
}
