'use client';

/*
 * Knowledge's workspace models and keys, on Settings → Models (B2b, D167 #4).
 *
 * The embedding model a knowledge base uses when it names none, the model that
 * writes Contextual Retrieval's contexts when a base names none (CR1, D180), the
 * reranker a search uses when a base names none, and the keys rerank providers need
 * (Cohere, Voyage, Jina, or a rerank server of your own: TEI or Infinity).
 * Keys are never served back (F201): a saved one reads "Saved", a blank field
 * keeps it, and Remove clears it. Saved with the rest of the tab.
 */
import { useState } from 'react';
import { Combobox, Field, Input, Section, type ComboboxOption } from '@/components/ui';
import { RERANK_SUGGESTIONS, useKnowledgeEnabled } from '@/lib/knowledge';
import { modelOptions, withUnreachable, type ProviderModels } from '@/lib/models';

import { FIELD_LABEL, FORM } from './form';
import { ProviderCard } from './ProviderCard';
import { SavedSecret, secretPlaceholder, useSavedSecrets } from './SavedSecret';

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

/** The rerankers on your own keys, each a provider card as on cloud's Rerankers (Naveen, M2g). Words are cloud's `RERANK_META`. */
const KEYS: { name: (typeof KNOWLEDGE_SECRETS)[number]; provider: string; label: string; placeholder: string; description: string }[] = [
    { name: 'cohere_key', provider: 'Cohere', label: 'Cohere API key', placeholder: 'For cohere.* rerank models',
      description: "Reranks a knowledge base's search results with Cohere Rerank on your own Cohere account, which bills it. Name a cohere.… model, such as cohere.rerank-v3.5." },
    { name: 'voyage_key', provider: 'Voyage AI', label: 'Voyage AI API key', placeholder: 'For voyage.* rerank models',
      description: "Reranks with Voyage's rerankers on your own Voyage account, which bills it. Name a voyage.… model, such as voyage.rerank-2.5 or voyage.rerank-2.5-lite." },
    { name: 'jina_key', provider: 'Jina AI', label: 'Jina AI API key', placeholder: 'For jina.* rerank models',
      description: "Reranks with Jina's multilingual rerankers on your own Jina account, which bills it. Name a jina.… model, such as jina.jina-reranker-v2-base-multilingual." },
];
/** A rerank server of your own (TEI or Infinity): the fourth card, holding its address and key. */
const SERVER = 'Rerank server (TEI or Infinity)';

export function KnowledgeModels({
    value, onChange, providers, embeddingModel, chatModel,
}: {
    value: KnowledgeModelSettings;
    onChange: (next: KnowledgeModelSettings) => void;
    providers: Record<string, ProviderModels>;
    /** The workspace's embedding model (code search), which Knowledge uses when it names none. */
    embeddingModel: string;
    /** The workspace's chat model, which writes Contextual Retrieval's contexts when nothing else is named. */
    chatModel: string;
}) {
    const enabled = useKnowledgeEnabled();
    const { secrets } = useSavedSecrets();
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

    return (
        <>
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
                <Field label="Rerank model" hint="Optional. Named with its provider, e.g. cohere.rerank-v3.5. Empty: no rerank unless a base names one. Cohere, Voyage and Jina need your key, under Rerankers below.">
                    <Input
                        value={value.knowledge_rerank_model}
                        onChange={e => set('knowledge_rerank_model', e.target.value.trim())}
                        placeholder="None"
                        list="knowledge-workspace-rerank-models"
                        autoComplete="off"
                        spellCheck={false}
                    />
                </Field>
                <datalist id="knowledge-workspace-rerank-models">
                    {RERANK_SUGGESTIONS.map(r => <option key={r.model} value={r.model}>{r.provider}</option>)}
                </datalist>
            </div>
            </div>
        </Section>

        <Section variant="plain" collapsible={false} title="Rerankers"
            description="Your own keys for reranking a knowledge base's search results, billed by the provider.">
            <div className={`${FORM} space-y-2`}>
                {KEYS.map(k => (
                    <ProviderCard key={k.name} name={k.provider} live={!!secrets[k.name]?.set}
                        status={secrets[k.name]?.set ? 'Key saved' : 'No key configured'}
                        open={open === k.name} onToggle={() => toggle(k.name)}>
                        <p className="text-xs text-text-muted">{k.description}</p>
                        <div className="space-y-1.5">
                            <label htmlFor={`rerank-${k.name}`} className={FIELD_LABEL}>{k.label}</label>
                            <Input
                                id={`rerank-${k.name}`}
                                type="password"
                                autoComplete="off"
                                value={value[k.name]}
                                onChange={e => set(k.name, e.target.value)}
                                placeholder={secretPlaceholder(secrets, k.name, k.placeholder)}
                            />
                            <SavedSecret name={k.name} label={k.label} typed={value[k.name]} />
                        </div>
                    </ProviderCard>
                ))}
                <ProviderCard name={SERVER} live={!!value.knowledge_rerank_base_url}
                    status={value.knowledge_rerank_base_url ? 'Address set' : 'Not set'}
                    open={open === 'server'} onToggle={() => toggle('server')}>
                    <Field label="Rerank server address" hint="Your own TEI or Infinity server, for tei.* models.">
                        <Input
                            value={value.knowledge_rerank_base_url}
                            onChange={e => set('knowledge_rerank_base_url', e.target.value.trim())}
                            placeholder="http://localhost:8080"
                            autoComplete="off"
                            spellCheck={false}
                        />
                    </Field>
                    <div className="space-y-1">
                        <Field label="Rerank server key">
                            <Input
                                type="password"
                                autoComplete="off"
                                value={value.knowledge_rerank_key}
                                onChange={e => set('knowledge_rerank_key', e.target.value)}
                                placeholder={secretPlaceholder(secrets, 'knowledge_rerank_key', 'Leave blank if not required')}
                            />
                        </Field>
                        <SavedSecret name="knowledge_rerank_key" label="rerank server key" typed={value.knowledge_rerank_key} />
                    </div>
                </ProviderCard>
            </div>
        </Section>
        </>
    );
}
