import { ExternalLink, RefreshCw } from 'lucide-react';
import { Button, Combobox, Field, Input, Section, Textarea } from '@/components/ui';
import { ProviderCard } from '@/components/providers/ProviderCard';
import { FORM } from './form';
import { modelOptions as buildModelOptions, providerLabel, withUnreachable } from '@/lib/models';

/** `builder_model` is empty when it should follow `model`; a Combobox needs a value. */
const DEFAULT_SENTINEL = '__default__';
import React, { useState } from 'react';
import { KnowledgeModels, type KnowledgeModelSettings } from './KnowledgeModels';
import type { RerankListing } from '@/lib/knowledge';
import { useSavedSecrets } from './SavedSecret';
import { KEY_LABELS, PROVIDER_META, cardMeta, cardStatus, secretOf, type ProviderInfo } from '@/lib/providerCards';

interface ModelsTabProps {
    providers: Record<string, ProviderInfo>;
    selectedModel: string; setSelectedModel: (v: string) => void;
    embeddingModel: string; setEmbeddingModel: (v: string) => void;
    builderModel: string; setBuilderModel: (v: string) => void;
    /** The builder's quick steps (its routing); empty follows the builder model (BWA1). */
    builderQuickModel: string; setBuilderQuickModel: (v: string) => void;
    /** The workspace response cap as typed; '' is automatic (F263). */
    maxOutputTokens: string; setMaxOutputTokens: (v: string) => void;
    /** The workspace reasoning effort, for agents that set none (F269). */
    reasoningEffort: string; setReasoningEffort: (v: string) => void;
    /** Served by the models endpoint: models with no Off, and where Off is the lowest level. */
    reasoningWithoutOff: string[]; reasoningOffMinimum: string[];
    awsRegion: string; setAwsRegion: (v: string) => void;
    loadingModels: boolean;
    /** Reads every provider's models again (the Providers section's Refresh). */
    onRefresh: () => void;
    onSave: () => void;
    isSaving?: boolean;
    openaiCompatibleBaseUrl: string; setOpenaiCompatibleBaseUrl: (v: string) => void;
    openaiCompatibleModels: string; setOpenaiCompatibleModels: (v: string) => void;
    openaiCompatibleEmbedModels: string; setOpenaiCompatibleEmbedModels: (v: string) => void;
    localCompatibleBaseUrl: string; setLocalCompatibleBaseUrl: (v: string) => void;
    localCompatibleModels: string; setLocalCompatibleModels: (v: string) => void;
    localCompatibleEmbedModels: string; setLocalCompatibleEmbedModels: (v: string) => void;
    huggingfaceModels: string; setHuggingfaceModels: (v: string) => void;
    anthropicCliModels: string; setAnthropicCliModels: (v: string) => void;
    geminiCliModels: string; setGeminiCliModels: (v: string) => void;
    codexCliModels: string; setCodexCliModels: (v: string) => void;
    githubCopilotCliModels: string; setGithubCopilotCliModels: (v: string) => void;
    /** Knowledge's default models and rerank keys (B2b), shown while Knowledge is on. */
    knowledgeModels: KnowledgeModelSettings; setKnowledgeModels: (v: KnowledgeModelSettings) => void;
    /** The rerank models the saved keys can use, per provider (BF9): the rerank picker and the reranker cards. */
    rerankListings: RerankListing[];
    // Backward compat
    mode: string; setMode: (v: string) => void;
    localModels: string[]; cloudModels: string[];
    filteredModels: string[];
}


/**
 * The providers in plain groups (Naveen, PROV1), each saying what it is for (GUIDE1, F912: "grouping heading can
 * be better"): who makes the models, who hosts others', what runs on hardware you control, and the CLIs you are
 * signed in to. Every card is in one (a test holds it); a provider missing from these lists still shows, at the end.
 */
const PROVIDER_GROUPS: { title: string; blurb: string; ids: string[] }[] = [
    { title: 'Model makers', blurb: 'A key from the company that makes the model.', ids: ['openai', 'anthropic', 'gemini', 'grok', 'deepseek', 'mistral', 'moonshot', 'qwen', 'zai', 'minimax'] },
    { title: 'Hosts', blurb: 'One key for many models made by others. Any OpenAI-compatible service goes here too.', ids: ['bedrock', 'openrouter', 'groq', 'cerebras', 'together', 'fireworks', 'nvidia', 'openai_compatible'] },
    { title: 'Your own machine or server', blurb: 'Models on hardware you control: nothing to pay per call, and nothing leaves it.', ids: ['ollama', 'local_compatible', 'huggingface'] },
    { title: 'CLI models', blurb: "Use the Claude, Gemini, Codex or Copilot CLI you're signed in to on this machine. No key needed.", ids: ['anthropic_cli', 'gemini_cli', 'codex_cli', 'github_copilot_cli'] },
];
const GROUPED = new Set(PROVIDER_GROUPS.flatMap(g => g.ids));
const PROVIDER_SECTIONS = PROVIDER_GROUPS.map((g, i) => ({
    ...g,
    ids: i === PROVIDER_GROUPS.length - 1
        ? [...g.ids, ...Object.keys(PROVIDER_META).filter(k => !GROUPED.has(k))]
        : g.ids,
}));

export const ModelsTab = ({
    providers, selectedModel, setSelectedModel,
    embeddingModel, setEmbeddingModel,
    builderModel, setBuilderModel,
    builderQuickModel, setBuilderQuickModel,
    maxOutputTokens, setMaxOutputTokens,
    reasoningEffort, setReasoningEffort, reasoningWithoutOff, reasoningOffMinimum,
    awsRegion, setAwsRegion, loadingModels, onRefresh,
    onSave, isSaving, mode, setMode, localModels, cloudModels, filteredModels,
    openaiCompatibleBaseUrl, setOpenaiCompatibleBaseUrl,
    openaiCompatibleModels, setOpenaiCompatibleModels,
    openaiCompatibleEmbedModels, setOpenaiCompatibleEmbedModels,
    localCompatibleBaseUrl, setLocalCompatibleBaseUrl,
    localCompatibleModels, setLocalCompatibleModels,
    localCompatibleEmbedModels, setLocalCompatibleEmbedModels,
    huggingfaceModels, setHuggingfaceModels,
    anthropicCliModels, setAnthropicCliModels,
    geminiCliModels, setGeminiCliModels,
    codexCliModels, setCodexCliModels,
    githubCopilotCliModels, setGithubCopilotCliModels,
    knowledgeModels, setKnowledgeModels, rerankListings,
}: ModelsTabProps) => {

    const { secrets, save: saveSecret, remove: removeSecret } = useSavedSecrets();
    const [expandedProvider, setExpandedProvider] = useState<string | null>(null);

    // Build all available models list for default selector
    const allAvailable: string[] = [];
    Object.entries(providers).forEach(([, info]) => {
        if (info.available) allAvailable.push(...info.models);
    });

    // Both lists, and the builder's picker, are built by one helper in
    // lib/models — they used to be three separate flatMaps that disagreed.
    const modelOptions = withUnreachable(buildModelOptions(providers), selectedModel);
    // Empty is a real choice (F1020's class: every optional picker can go back to empty): code search and each
    // step's semantic cache simply have no embeddings then.
    const embeddingOptions = [
        { value: DEFAULT_SENTINEL, label: 'None' },
        ...withUnreachable(buildModelOptions(providers, 'embedding_models'), embeddingModel),
    ];
    // Empty means "whatever the default model is", which is a real choice and
    // needs a row of its own — a blank control reads as nothing being set.
    const builderOptions = [
        { value: DEFAULT_SENTINEL, label: `Same as default model${selectedModel ? ` (${selectedModel})` : ''}` },
        ...withUnreachable(buildModelOptions(providers), builderModel),
    ];
    const quickOptions = [
        { value: DEFAULT_SENTINEL, label: `Same as builder model${(builderModel || selectedModel) ? ` (${builderModel || selectedModel})` : ''}` },
        ...withUnreachable(buildModelOptions(providers), builderQuickModel),
    ];

    /** A card's other fields save with the page, from a Save of their own inside the card. */
    const saveHere = (
        <div className="flex justify-end">
            <Button variant="secondary" onClick={onSave} loading={isSaving}>Save</Button>
        </div>
    );
    const cliCustom: Record<string, { value: string; set: (v: string) => void; placeholder: string; prefix: string }> = {
        anthropic_cli: { value: anthropicCliModels, set: setAnthropicCliModels, placeholder: 'e.g. claude-opus-4-5-20251101', prefix: 'cli.claude' },
        gemini_cli: { value: geminiCliModels, set: setGeminiCliModels, placeholder: 'e.g. gemini-2.5-pro', prefix: 'cli.gemini' },
        codex_cli: { value: codexCliModels, set: setCodexCliModels, placeholder: 'e.g. gpt-5.4, gpt-5.4-mini', prefix: 'cli.codex' },
        github_copilot_cli: { value: githubCopilotCliModels, set: setGithubCopilotCliModels, placeholder: 'e.g. claude-sonnet-4-5, gpt-4.1', prefix: 'cli.copilot' },
    };
    const CLI_INSTALL: Record<string, { label: string; url: string }> = {
        anthropic_cli: { label: 'Install Claude CLI →', url: 'https://claude.ai/download' },
        gemini_cli: { label: 'Install Gemini CLI →', url: 'https://ai.google.dev/gemini-api/docs/gemini-cli' },
        codex_cli: { label: 'Install OpenAI Codex CLI →', url: 'https://github.com/openai/codex' },
        github_copilot_cli: { label: 'Install GitHub Copilot CLI →', url: 'https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli' },
    };

    /** What a provider needs beyond its key, in its card. */
    const extrasFor = (key: string, providerData: ProviderInfo): React.ReactNode => {
        const modelCount = providerData.models.length;
        if (key === 'bedrock') return (
            <div className="space-y-3">
                <Field label="AWS region">
                    <Input id="awsregion" type="text" value={awsRegion} onChange={e => setAwsRegion(e.target.value)} placeholder="us-east-1" />
                </Field>
                {/* A profile is a model since batch 1B: discovery lists each as `bedrock.<id>`, and the
                    workspace-wide override this card used to set is no longer applied (F66). */}
                <p className="text-2xs text-text-faint leading-relaxed">
                    Inference profiles are listed as models — the <span className="font-mono">us.</span>,{' '}
                    <span className="font-mono">eu.</span>, <span className="font-mono">apac.</span> and{' '}
                    <span className="font-mono">global.</span> variants and your application profiles. Pick one
                    wherever you choose a model; a model with no on-demand throughput is only available that way.
                </p>
                {saveHere}
            </div>
        );
        if (key === 'openai_compatible' || key === 'local_compatible') {
            const local = key === 'local_compatible';
            return (
                <div className="space-y-3">
                    <Field label="Base URL" hint="The /v1 path is appended automatically. Do not include /v1 in the URL.">
                        <Input id={local ? 'localcompatiblebaseurl' : 'openaicompatiblebaseurl'} type="text"
                            value={local ? localCompatibleBaseUrl : openaiCompatibleBaseUrl}
                            onChange={e => (local ? setLocalCompatibleBaseUrl : setOpenaiCompatibleBaseUrl)(e.target.value)}
                            placeholder={local ? 'http://localhost:8000' : 'https://openrouter.ai/api'} />
                    </Field>
                    <Field label="Model names (comma-separated)" hint="If the /v1/models endpoint is available, models will be fetched automatically. Otherwise, list them here.">
                        <Input id={local ? 'localcompatiblemodels' : 'openaicompatiblemodels'} type="text"
                            value={local ? localCompatibleModels : openaiCompatibleModels}
                            onChange={e => (local ? setLocalCompatibleModels : setOpenaiCompatibleModels)(e.target.value)}
                            placeholder={local ? 'e.g. llama-3-70b, mistral-7b' : 'e.g. meta-llama/llama-3-70b-instruct, google/gemma-2-27b-it'} />
                    </Field>
                    <Field label="Embedding model names (comma-separated)" hint={'Models listed here appear in the embedding model dropdown. Models with "embed" in the name are also auto-detected from /v1/models.'}>
                        <Input id={local ? 'localcompatibleembedmodels' : 'openaicompatibleembedmodels'} type="text"
                            value={local ? localCompatibleEmbedModels : openaiCompatibleEmbedModels}
                            onChange={e => (local ? setLocalCompatibleEmbedModels : setOpenaiCompatibleEmbedModels)(e.target.value)}
                            placeholder={local ? 'e.g. bge-m3' : 'e.g. hf:nomic-ai/nomic-embed-text-v1.5'} />
                    </Field>
                    {saveHere}
                </div>
            );
        }
        if (key === 'huggingface') return (
            <div className="space-y-3">
                <Field label="Model IDs (one per line or comma-separated)" hint={<>Each ID becomes a selectable <code className="font-code text-text-muted">hf.&lt;org&gt;/&lt;model&gt;</code> model. First call to a model pays a 20-60s load cost, then stays warm in memory.</>}>
                    <Textarea id="huggingfacemodels" value={huggingfaceModels} onChange={e => setHuggingfaceModels(e.target.value)} rows={4}
                        className="font-mono"
                        placeholder={"Qwen/Qwen2.5-7B-Instruct\nmeta-llama/Llama-3.1-8B-Instruct\nmistralai/Mistral-7B-Instruct-v0.3"} />
                </Field>
                {saveHere}
            </div>
        );
        if (key === 'ollama') return (
            <div className="space-y-1.5">
                <div className="text-2xs text-text-faint">
                    {providerData.available
                        ? `Detected ${modelCount} local model${modelCount !== 1 ? 's' : ''}: ${providerData.models.slice(0, 5).join(', ')}${modelCount > 5 ? '...' : ''}`
                        : 'Ollama is not running. Start it to use local models.'}
                </div>
                <a href="https://ollama.com/download" target="_blank" rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline">
                    <ExternalLink className="h-2.5 w-2.5" />
                    Download Ollama →
                </a>
            </div>
        );
        if (key.endsWith('_cli')) {
            const cfg = cliCustom[key];
            const install = CLI_INSTALL[key];
            const cli = providerData.cli;
            return (
                <div className="space-y-2">
                    <div className="text-2xs text-text-faint" data-cli-state={key}>
                        {!providerData.available
                            ? `${providerLabel(key)} is not installed: no binary for it was found in PATH.`
                            : [
                                cli?.version ? `Version ${cli.version}` : 'Installed',
                                cli?.signed_in === true ? 'signed in' : cli?.signed_in === false ? 'not signed in' : null,
                                cli?.models_note,
                            ].filter(Boolean).join(' · ')}
                    </div>
                    {providerData.available && cli?.signed_in === false && cli.sign_in_hint && (
                        <div className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5 text-xs leading-relaxed text-text">
                            <strong>Not signed in.</strong> {cli.sign_in_hint} Then press Refresh.
                        </div>
                    )}
                    <div className="rounded-md bg-surface-2 px-3 py-2.5 text-xs leading-relaxed text-text-muted">
                        <strong>No API key needed</strong> — uses your existing CLI session. Run the CLI manually first to authenticate.
                        {' '}Synapse runs it for its model only: none of its own tools, MCP servers, hooks, skills or memory files.
                    </div>
                    {providerData.available && cli?.note && (
                        <p className="text-2xs leading-relaxed text-text-faint">{cli.note}</p>
                    )}
                    {install && (
                        <a href={install.url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline">
                            <ExternalLink className="h-2.5 w-2.5" /> {install.label}
                        </a>
                    )}
                    {cfg && (
                        <Field label="Custom model names (comma-separated)" hint={<>Each name is passed to the CLI&apos;s <code className="font-code text-text-muted">-m</code> flag and becomes a selectable <code className="font-code text-text-muted">{cfg.prefix}.&lt;name&gt;</code> model. Use this when the CLI&apos;s default model isn&apos;t available to your account.</>}>
                            <Input id={`cli-models-${key}`} type="text" value={cfg.value} onChange={e => cfg.set(e.target.value)} placeholder={cfg.placeholder} />
                        </Field>
                    )}
                    {cfg && saveHere}
                </div>
            );
        }
        return null;
    };

    return (
        <div>
            {/* The keys first (Naveen, GUIDE1 F912; was PROV1's defaults-first order): a new workspace has
                nothing for Defaults to pick until a provider is in. Then Defaults, Knowledge, the rerankers. */}
            {/* Provider cards, cloud's card synced (PROV1): each saves and removes its own key. */}
            <Section variant="plain" collapsible={false} title="Providers"
                description="Bring your own keys. Add a key for any provider below, or use a model on your own machine or a CLI you're signed in to, and its models show up wherever you pick a model. Keys are kept in this install and never shown again: only a fragment, so you can tell which is in place."
                actions={
                    <Button variant="secondary" size="sm" onClick={onRefresh} loading={loadingModels}>
                        <RefreshCw className="size-3.5" aria-hidden /> Refresh
                    </Button>
                }>
                <div className={`${FORM} space-y-6`}>
                    {PROVIDER_SECTIONS.map(group => (
                    <div key={group.title} className="space-y-2" data-provider-group={group.title}>
                    <div>
                        <h3 className="text-xs font-medium text-text">{group.title}</h3>
                        <p className="mt-0.5 text-xs text-text-faint">{group.blurb}</p>
                    </div>
                    {group.ids.filter(key => key in PROVIDER_META).map(key => {
                        const providerData = providers[key] || { available: false, models: [] };
                        const secret = secretOf(key);
                        const stored = secret ? secrets[secret] : undefined;
                        return (
                            <ProviderCard
                                key={key}
                                meta={cardMeta(key)}
                                {...cardStatus(key, providerData)}
                                error={providerData.error}
                                keyState={stored ? { set: stored.set, hint: stored.hint ? `…${stored.hint}` : null } : undefined}
                                keyLabel={KEY_LABELS[key]}
                                onSaveKey={secret ? (v: string) => saveSecret(secret, v) : undefined}
                                onRemoveKey={secret ? async () => { await removeSecret(secret); } : undefined}
                                extras={extrasFor(key, providerData)}
                                models={key === 'ollama' ? [] : providerData.models}
                                open={expandedProvider === key}
                                onToggle={() => setExpandedProvider(expandedProvider === key ? null : key)}
                            />
                        );
                    })}
                    </div>
                    ))}
                </div>
            </Section>

            <Section variant="plain" collapsible={false} title="Defaults"
                description="What an agent or a step uses when it names no model of its own.">
              <div className={`${FORM} space-y-5`}>
                <Field label="Default model" hint="Used for system prompt generation and agents without a specific model assigned.">
                    <Combobox
                        value={selectedModel || undefined}
                        onChange={setSelectedModel}
                        options={modelOptions}
                        placeholder={loadingModels ? 'Loading models…' : 'Select default model…'}
                        searchPlaceholder="Search models…"
                        aria-label="Default model"
                    />
                </Field>
                {/* `builder_model` has been writable and honoured end to end since the builder shipped;
                    the only other way to set it is "Set as default" inside the panel. */}
                <Field label="Builder model" hint="Used by Synapse AI for every step of a build, except the quick steps when you pick a model for them below. Reasoning-capable models do noticeably better here.">
                    <Combobox
                        value={builderModel || DEFAULT_SENTINEL}
                        onChange={(v) => setBuilderModel(v === DEFAULT_SENTINEL ? '' : v)}
                        options={builderOptions}
                        placeholder={loadingModels ? 'Loading models…' : 'Select builder model…'}
                        searchPlaceholder="Search models…"
                        aria-label="Builder model"
                    />
                </Field>
                {/* BWA1: the builder's quick steps — deciding what a message asks for, whether a plan was
                    approved — on their own model, often a small one. Empty follows the builder model.
                    Helpers set to "Quick-step model" run on it too (SUB1a), hence not the builder's alone. */}
                <Field label="Model for quick steps" hint="Used by Synapse AI to decide what you asked for and whether you approved a plan, and by an agent's helpers when its Helper model is the quick-step model. A small, fast model usually does this as well as a large one.">
                    <Combobox
                        value={builderQuickModel || DEFAULT_SENTINEL}
                        onChange={(v) => setBuilderQuickModel(v === DEFAULT_SENTINEL ? '' : v)}
                        options={quickOptions}
                        placeholder={loadingModels ? 'Loading models…' : 'Select a model for quick steps…'}
                        searchPlaceholder="Search models…"
                        aria-label="Model for quick steps"
                    />
                </Field>
                {/* Reasoning effort — the workspace default (F269). Every agent with none of its own runs
                    at it. All four levels stay: it reaches every model, not only the default one. */}
                <div>
                    {/* Labelled as a kit `Field` is: the segmented control is not one input (D231). */}
                    <p className="mb-1.5 text-sm font-medium text-text">Reasoning effort</p>
                    <div className="flex gap-1" role="group" aria-label="Reasoning effort" aria-describedby="models-effort-help">
                        {['off', 'low', 'medium', 'high'].map(level => {
                            const active = (reasoningEffort || 'medium') === level;
                            return (
                                <button
                                    key={level}
                                    type="button"
                                    onClick={() => setReasoningEffort(level)}
                                    aria-pressed={active}
                                    className={`flex-1 rounded-md border px-2 py-1.5 text-xs font-medium capitalize transition-colors ${
                                        active
                                            ? 'border-accent bg-accent-subtle text-text'
                                            : 'border-border bg-surface text-text-muted hover:border-border-strong hover:text-text'
                                    }`}
                                >
                                    {level}
                                </button>
                            );
                        })}
                    </div>
                    {reasoningOffMinimum.includes(selectedModel) && (
                        <p className="mt-1.5 text-2xs text-text-muted">{selectedModel} can&apos;t stop thinking entirely: Off runs at its lowest thinking level.</p>
                    )}
                    {reasoningWithoutOff.includes(selectedModel) && (
                        <p className="mt-1.5 text-2xs text-text-muted">{selectedModel} has no Off: the least it thinks is Low.</p>
                    )}
                    <p id="models-effort-help" className="mt-1.5 text-xs leading-relaxed text-text-faint">
                        How much a model that can think does before it answers, for every agent that sets none of
                        its own. Higher costs more and takes longer; thinking tokens are billed as output.
                    </p>
                </div>
                {/* Max response length (F263). The "raise the response cap" error points here. */}
                <Field label="Max response length" hint="The most a model may write in one response, thinking included, in tokens. Blank is automatic: 8,192 for most providers and no limit for OpenAI, OpenAI-compatible and local servers. A model that thinks always gets room past its thinking. An agent can set its own.">
                    <Input id="models-max-output"
                        type="number"
                        inputMode="numeric"
                        min={256}
                        max={262144}
                        step={1024}
                        placeholder="Automatic"
                        value={maxOutputTokens}
                        onChange={e => setMaxOutputTokens(e.target.value)}
                        aria-label="Max response length"
                        className="w-48 tabular-nums"
                    />
                </Field>
                <Field label="Embedding model" hint="Indexes code repositories, matches each workflow step's semantic cache, and embeds knowledge bases unless Knowledge has its own. Gemini, OpenAI, Bedrock, Ollama and OpenAI-compatible servers embed.">
                    <Combobox
                        value={embeddingModel || DEFAULT_SENTINEL}
                        onChange={(v) => setEmbeddingModel(v === DEFAULT_SENTINEL ? '' : v)}
                        options={embeddingOptions}
                        placeholder={loadingModels ? 'Loading models…' : 'Select default embedding model…'}
                        searchPlaceholder="Search embedding models…"
                        aria-label="Embedding model"
                    />
                </Field>
                <div className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5">
                    <p className="text-xs leading-relaxed text-text">
                        <strong className="font-semibold">Warning:</strong>{' '}Changing the global embedding model will affect new repository indexes.
                        Existing repositories will NOT be automatically migrated. Use individual repo settings to re-index manually if needed.
                        Each step&apos;s semantic cache starts afresh.
                    </p>
                </div>
              </div>
            </Section>

            <KnowledgeModels part="knowledge" value={knowledgeModels} onChange={setKnowledgeModels} providers={providers}
                rerankListings={rerankListings}
                embeddingModel={embeddingModel} chatModel={selectedModel} />

            {/* The form's one save, at its foot under the last field, on the right of its column: no rule
                of its own, so it reads as part of Defaults and Knowledge, not a section before the rerankers. */}
            <div className={`${FORM} flex justify-end pb-5`}>
                <Button onClick={onSave} loading={isSaving}>{isSaving ? 'Saving…' : 'Save changes'}</Button>
            </div>

            <KnowledgeModels part="rerankers" value={knowledgeModels} onChange={setKnowledgeModels} providers={providers}
                rerankListings={rerankListings}
                embeddingModel={embeddingModel} chatModel={selectedModel} onSave={onSave} isSaving={isSaving} />
        </div>
    );
};
