'use client';

/*
 * OSS's answers to the Knowledge screens' questions (`components/knowledge/host.tsx`).
 *
 * The workspace's defaults and keys are plain settings here (`/api/settings`),
 * models are grouped by `@/lib/models`, the rerank models are what the saved keys
 * (or your own server) can use (`/api/models/rerank`, BF9), and part URLs never expire. Cloud keeps its own copy of
 * this file; the screens themselves are shared.
 */
import type { ReactNode } from 'react';

import {
    DEFAULT_KNOWLEDGE_HOST, KnowledgeHostProvider, type KnowledgeHost, type KnowledgeWorkspace,
} from '@/components/knowledge/host';
import { rerankOptions, type RerankListing } from '@/lib/knowledge';
import { modelOptions, withUnreachable, type ProviderModels } from '@/lib/models';

const RERANK_KEYS = ['cohere_key', 'voyage_key', 'jina_key', 'model_gateway_key'];

async function loadWorkspace(): Promise<KnowledgeWorkspace> {
    const [settings, models, rerank] = await Promise.all([
        fetch('/api/settings').then(r => r.json()).catch(() => ({})),
        fetch('/api/models').then(r => r.json()).catch(() => ({})),
        fetch('/api/models/rerank').then(r => (r.ok ? r.json() : null)).catch(() => null) as Promise<{ providers?: RerankListing[] } | null>,
    ]);
    const listings: RerankListing[] = Array.isArray(rerank?.providers) ? rerank.providers : [];
    const providers: Record<string, ProviderModels> = models?.providers ?? {};
    const keys: Record<string, boolean> = {};
    for (const k of RERANK_KEYS) keys[k] = !!settings?.[`${k}_set`];
    keys.knowledge_rerank_base_url = !!settings?.knowledge_rerank_base_url;
    const judge = String(settings?.judge_model || '');
    return {
        embedding: String(settings?.knowledge_embedding_model || settings?.embedding_model || ''),
        embeddingModels: modelOptions(providers, 'embedding_models'),
        chatModels: modelOptions(providers),
        model: String(settings?.model || ''),
        judge: judge === 'default' ? '' : judge,
        contextual: String(settings?.knowledge_contextual_model || settings?.model || ''),
        rerank: String(settings?.knowledge_rerank_model || ''),
        rerankModels: rerankOptions(listings),
        keys,
    };
}

export const ossKnowledgeHost: KnowledgeHost = {
    ...DEFAULT_KNOWLEDGE_HOST,
    loadWorkspace,
    withUnreachable,
    offHint: 'Turn it on in Settings → General to make Vault folders searchable.',
};

export function OssKnowledgeHost({ children }: { children: ReactNode }) {
    return <KnowledgeHostProvider value={ossKnowledgeHost}>{children}</KnowledgeHostProvider>;
}
