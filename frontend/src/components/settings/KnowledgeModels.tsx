'use client';

/*
 * Knowledge's workspace models and keys, on Settings → Models (B2b, D167 #4).
 *
 * The embedding model a knowledge base uses when it names none, the model that
 * writes Contextual Retrieval's contexts when a base names none (CR1, D180), the
 * reranker a search uses when a base names none, and the keys rerank providers need
 * (Cohere, Voyage, Jina, or a rerank server of your own: TEI or Infinity). The rerank
 * picker and each card list what that key can use (`/api/models/rerank`, BF9).
 * Keys are never served back (F201). The models save with the rest of the tab;
 * the rerankers are provider cards that save and remove their own keys, as the
 * model providers' do (cloud's card, synced; PROV1). Drawn in two parts, so the
 * page reads in cloud's order: Knowledge, the providers, then the rerankers.
 */
import { useState } from 'react';
import { Button, Combobox, Field, Input, Section, type ComboboxOption } from '@/components/ui';
import { ProviderCard } from '@/components/providers/ProviderCard';
import { rerankOptions, rerankStatus, useKnowledgeEnabled, type RerankListing } from '@/lib/knowledge';
import { modelOptions, withUnreachable, type ProviderModels } from '@/lib/models';

import { FORM } from './form';
import { useSavedSecrets } from './SavedSecret';

export type KnowledgeModelSettings = {
    knowledge_embedding_model: string;
    knowledge_contextual_model: string;
    knowledge_rerank_model: string;
    cohere_key: string;
    voyage_key: string;
    jina_key: string;
    knowledge_rerank_base_url: string;
    knowledge_rerank_key: string;
};

export const KNOWLEDGE_MODEL_DEFAULTS: KnowledgeModelSettings = {
    knowledge_embedding_model: '', knowledge_contextual_model: '', knowledge_rerank_model: '', cohere_key: '',
    voyage_key: '', jina_key: '',
    knowledge_rerank_base_url: '', knowledge_rerank_key: '',
};

/** The secrets among them, which `GET /api/settings` serves only as `<name>_set` and `<name>_hint`. */
export const KNOWLEDGE_SECRETS = ['cohere_key', 'voyage_key', 'jina_key', 'knowledge_rerank_key'] as const;

const SAME = '__same__';
const NONE = '__none__';

/** The rerankers on your own keys, each a provider card as on cloud's Rerankers (Naveen, M2g). Words are cloud's `RERANK_META`. */
const KEYS: { name: (typeof KNOWLEDGE_SECRETS)[number]; provider: string; label: string; placeholder: string; description: string }[] = [
    { name: 'cohere_key', provider: 'Cohere', label: 'Cohere API key', placeholder: 'For cohere.* rerank models',
      description: "Reranks a knowledge base's search results with Cohere Rerank on your own Cohere account, which bills it. Its rerank models are listed once the key is saved." },
    { name: 'voyage_key', provider: 'Voyage AI', label: 'Voyage AI API key', placeholder: 'For voyage.* rerank models',
      description: "Reranks with Voyage's rerankers on your own Voyage account, which bills it. Its rerank models are listed once the key is saved." },
    { name: 'jina_key', provider: 'Jina AI', label: 'Jina AI API key', placeholder: 'For jina.* rerank models',
      description: "Reranks with Jina's multilingual rerankers on your own Jina account, which bills it. Its rerank models are listed once the key is saved." },
];
/** A rerank server of your own (TEI or Infinity): the fourth card, holding its address and key. */
const SERVER = 'Rerank server (TEI or Infinity)';

export function KnowledgeModels({
    part, value, onChange, providers, rerankListings, embeddingModel, chatModel, onSave, isSaving,
}: {
    /** Which half to draw: the models, or the rerankers' cards. */
    part: 'knowledge' | 'rerankers';
    value: KnowledgeModelSettings;
    onChange: (next: KnowledgeModelSettings) => void;
    providers: Record<string, ProviderModels>;
    /** The rerank models each saved key (or the rerank server) can use, or why none (BF9). */
    rerankListings: RerankListing[];
    /** The workspace's embedding model (code search), which Knowledge uses when it names none. */
    embeddingModel: string;
    /** The workspace's chat model, which writes Contextual Retrieval's contexts when nothing else is named. */
    chatModel: string;
    /** The page's save, for the rerank server's address (its key saves in its card). */
    onSave?: () => void;
    isSaving?: boolean;
}) {
    const enabled = useKnowledgeEnabled();
    const { secrets, save, remove } = useSavedSecrets();
    const [open, setOpen] = useState<string | null>(null);
    if (!enabled) return null;
    const toggle = (k: string) => setOpen(o => (o === k ? null : k));

    const set = (field: keyof KnowledgeModelSettings, v: string) => onChange({ ...value, [field]: v });
    const embeddingOptions: ComboboxOption[] = [
        { value: SAME, label: embeddingModel ? `Same as the embedding model above (${embeddingModel})` : 'Same as the embedding model above (none set)' },
        ...withUnreachable(modelOptions(providers, 'embedding_models'), value.knowledge_embedding_model || undefined),
    ];
    const contextualOptions: ComboboxOption[] = [
        { value: SAME, label: chatModel ? `Same as the default model above (${chatModel})` : 'Same as the default model above (none set)' },
        ...withUnreachable(modelOptions(providers), value.knowledge_contextual_model || undefined),
    ];
    const rerankChoices: ComboboxOption[] = [
        { value: NONE, label: 'None: no rerank unless a base names one' },
        ...withUnreachable(rerankOptions(rerankListings), value.knowledge_rerank_model || undefined),
    ];
    const listing = (key: string) => rerankListings.find(l => l.key === key);

    if (part === 'rerankers') return (
        <Section variant="plain" collapsible={false} title="Rerankers"
            description="Your own keys for reranking a knowledge base's search results, billed by the provider.">
            <div className={`${FORM} space-y-2`}>
                {KEYS.map(k => (
                    <ProviderCard
                        key={k.name}
                        meta={{ label: k.provider, description: k.description, keyPlaceholder: k.placeholder }}
                        live={!!secrets[k.name]?.set && !listing(k.name)?.error}
                        status={secrets[k.name]?.set ? rerankStatus(listing(k.name), 'Key saved') : 'No key configured'}
                        error={secrets[k.name]?.set ? listing(k.name)?.error : null}
                        models={listing(k.name)?.models.map(m => m.id) ?? []}
                        keyState={{ set: !!secrets[k.name]?.set, hint: secrets[k.name]?.hint ? `…${secrets[k.name].hint}` : null }}
                        keyLabel={k.label}
                        onSaveKey={(v: string) => save(k.name, v)}
                        onRemoveKey={async () => { await remove(k.name); }}
                        removeNote="Searches that rerank with it keep their search order instead, and say so."
                        open={open === k.name}
                        onToggle={() => toggle(k.name)}
                    />
                ))}
                <ProviderCard
                    meta={{ label: SERVER, description: 'Your own TEI or Infinity server, for tei.* models.', keyPlaceholder: 'Leave blank if not required' }}
                    live={!!value.knowledge_rerank_base_url && !listing('knowledge_rerank_base_url')?.error}
                    status={value.knowledge_rerank_base_url ? rerankStatus(listing('knowledge_rerank_base_url'), 'Address set') : 'Not set'}
                    error={value.knowledge_rerank_base_url ? listing('knowledge_rerank_base_url')?.error : null}
                    models={listing('knowledge_rerank_base_url')?.models.map(m => m.id) ?? []}
                    keyState={{ set: !!secrets.knowledge_rerank_key?.set, hint: secrets.knowledge_rerank_key?.hint ? `…${secrets.knowledge_rerank_key.hint}` : null }}
                    keyLabel="Rerank server key"
                    onSaveKey={(v: string) => save('knowledge_rerank_key', v)}
                    onRemoveKey={async () => { await remove('knowledge_rerank_key'); }}
                    removeNote="Searches that rerank with it keep their search order instead, and say so."
                    open={open === 'server'}
                    onToggle={() => toggle('server')}
                    extras={
                        <div className="space-y-3">
                            <Field label="Rerank server address" hint="Your own TEI or Infinity server, for tei.* models.">
                                <Input
                                    value={value.knowledge_rerank_base_url}
                                    onChange={e => set('knowledge_rerank_base_url', e.target.value.trim())}
                                    placeholder="http://localhost:8080"
                                    autoComplete="off"
                                    spellCheck={false}
                                />
                            </Field>
                            {onSave && (
                                <div className="flex justify-end">
                                    <Button variant="secondary" onClick={onSave} loading={isSaving}>Save</Button>
                                </div>
                            )}
                        </div>
                    }
                />
            </div>
        </Section>
    );

    return (
        <Section variant="plain" collapsible={false} title="Knowledge">
            <div className={`${FORM} space-y-4`}>
                <p className="text-sm text-text-muted">
                    The models knowledge bases use unless a base names its own, and the keys rerank providers need.
                    Changing the embedding model rebuilds only the bases that use the default, the next time their
                    settings change.
                </p>
            <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Embedding model for knowledge" hint="Search by meaning. Without one, knowledge is searched by words.">
                    <Combobox
                        value={value.knowledge_embedding_model || SAME}
                        onChange={v => set('knowledge_embedding_model', v === SAME ? '' : v)}
                        options={embeddingOptions}
                        searchPlaceholder="Search embedding models…"
                        aria-label="Embedding model for knowledge"
                    />
                </Field>
                <Field label="Contextual Retrieval model" hint="Writes each passage's context in bases that turn Contextual Retrieval on and name no model. A small, inexpensive model is enough.">
                    <Combobox
                        value={value.knowledge_contextual_model || SAME}
                        onChange={v => set('knowledge_contextual_model', v === SAME ? '' : v)}
                        options={contextualOptions}
                        searchPlaceholder="Search chat models…"
                        aria-label="Contextual Retrieval model"
                    />
                </Field>
                <Field label="Rerank model" hint={rerankListings.some(l => l.models.length)
                    ? 'Optional. The models your rerank keys can use; a base can name its own.'
                    : 'None to pick yet: save a key under Rerankers below, and its models are listed here.'}>
                    <Combobox
                        value={value.knowledge_rerank_model || NONE}
                        onChange={v => set('knowledge_rerank_model', v === NONE ? '' : v)}
                        options={rerankChoices}
                        searchPlaceholder="Search rerank models…"
                        aria-label="Rerank model"
                    />
                </Field>
            </div>
            </div>
        </Section>
    );
}
