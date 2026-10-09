import { ExternalLink, Eye, EyeOff } from 'lucide-react';
import { Combobox, Input, Label, Section, Textarea } from '@/components/ui';
import { FIELD_LABEL, FORM } from './form';
import { ProviderCard } from './ProviderCard';
import { SaveBar } from './SaveBar';
import { modelOptions as buildModelOptions, providerLabel, withUnreachable } from '@/lib/models';

/** `builder_model` is empty when it should follow `model`; a Combobox needs a value. */
const DEFAULT_SENTINEL = '__default__';
import React, { useState } from 'react';
import { KnowledgeModels, type KnowledgeModelSettings } from './KnowledgeModels';
import { SavedSecret, secretPlaceholder, useSavedSecrets } from './SavedSecret';

// Every brand mark is decoration: it sits beside the provider's own name, so an
// `alt` would make a screen reader read that name twice — and the Local V1
// Compatible card borrows Ollama's mark, so it read out the wrong name entirely.
// Keep these silent; the visible label is what names the card (F44). A provider
// with no logo of ours is a theme-aware letter tile (`AppMark`, M2f), not a
// brand hex that stays one colour on both grounds (M2g).
const OLLAMA_IMG = '/ollama-icon.svg';

interface ProviderInfo {
    available: boolean;
    models: string[];
    embedding_models?: string[];
}

interface ModelsTabProps {
    providers: Record<string, ProviderInfo>;
    selectedModel: string; setSelectedModel: (v: string) => void;
    embeddingModel: string; setEmbeddingModel: (v: string) => void;
    builderModel: string; setBuilderModel: (v: string) => void;
    /** The workspace response cap as typed; '' is automatic (F263). */
    maxOutputTokens: string; setMaxOutputTokens: (v: string) => void;
    /** The workspace reasoning effort, for agents that set none (F269). */
    reasoningEffort: string; setReasoningEffort: (v: string) => void;
    /** Served by the models endpoint: models with no Off, and where Off is the lowest level. */
    reasoningWithoutOff: string[]; reasoningOffMinimum: string[];
    openaiKey: string; setOpenaiKey: (v: string) => void;
    anthropicKey: string; setAnthropicKey: (v: string) => void;
    geminiKey: string; setGeminiKey: (v: string) => void;
    grokKey: string; setGrokKey: (v: string) => void;
    deepseekKey: string; setDeepseekKey: (v: string) => void;
    bedrockApiKey: string; setBedrockApiKey: (v: string) => void;
    awsRegion: string; setAwsRegion: (v: string) => void;
    loadingModels: boolean;
    onSave: () => void;
    isSaving?: boolean;
    openaiCompatibleKey: string; setOpenaiCompatibleKey: (v: string) => void;
    openaiCompatibleBaseUrl: string; setOpenaiCompatibleBaseUrl: (v: string) => void;
    openaiCompatibleModels: string; setOpenaiCompatibleModels: (v: string) => void;
    openaiCompatibleEmbedModels: string; setOpenaiCompatibleEmbedModels: (v: string) => void;
    localCompatibleBaseUrl: string; setLocalCompatibleBaseUrl: (v: string) => void;
    localCompatibleKey: string; setLocalCompatibleKey: (v: string) => void;
    localCompatibleModels: string; setLocalCompatibleModels: (v: string) => void;
    localCompatibleEmbedModels: string; setLocalCompatibleEmbedModels: (v: string) => void;
    huggingfaceToken: string; setHuggingfaceToken: (v: string) => void;
    huggingfaceModels: string; setHuggingfaceModels: (v: string) => void;
    anthropicCliModels: string; setAnthropicCliModels: (v: string) => void;
    geminiCliModels: string; setGeminiCliModels: (v: string) => void;
    codexCliModels: string; setCodexCliModels: (v: string) => void;
    githubCopilotCliModels: string; setGithubCopilotCliModels: (v: string) => void;
    /** Knowledge's default models and rerank keys (B2b), shown while Knowledge is on. */
    knowledgeModels: KnowledgeModelSettings; setKnowledgeModels: (v: KnowledgeModelSettings) => void;
    // Backward compat
    mode: string; setMode: (v: string) => void;
    localModels: string[]; cloudModels: string[];
    filteredModels: string[];
}

interface ProviderMeta {
    /** A logo of ours, or none: a letter tile. */
    img?: string;
    description: string;
    keyPlaceholder?: string;
    /** Short link label and URL for getting an API key */
    keyLink?: { label: string; url: string };
    /** Extra human-readable note about the key */
    keyNote?: string;
}

const PROVIDER_META: Record<string, ProviderMeta> = {
    ollama: {
        img: OLLAMA_IMG,
        description: 'Runs locally on your machine. Private, free, no API key needed.',
    },
    huggingface: {
        description: 'Run HuggingFace transformers models directly on the host machine. Requires torch + transformers installed and (for large models) a GPU.',
        keyPlaceholder: 'hf_...',
        keyLink: { label: 'Get a token at HuggingFace →', url: 'https://huggingface.co/settings/tokens' },
        keyNote: 'Token is only needed for gated models (Llama, Gemma, etc.). Public models work without it.',
    },
    gemini: {
        img: '/google-gemini-icon.svg',
        description: 'Google AI models. Fast and capable, with a generous free tier.',
        keyPlaceholder: 'AIza...',
        keyLink: { label: 'Get a free key at Google AI Studio →', url: 'https://aistudio.google.com/app/apikey' },
        keyNote: 'Free tier available. Key starts with AIza...',
    },
    anthropic: {
        img: '/claude-ai-icon.svg',
        description: 'Advanced reasoning and analysis via Claude models.',
        keyPlaceholder: 'sk-ant-...',
        keyLink: { label: 'Get your key at Anthropic Console →', url: 'https://console.anthropic.com/settings/keys' },
        keyNote: 'Key starts with sk-ant-api03-...',
    },
    openai: {
        img: '/chatgpt-icon.svg',
        description: 'GPT-4o and latest OpenAI models.',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at OpenAI Platform →', url: 'https://platform.openai.com/api-keys' },
        keyNote: 'Key starts with sk-proj-... or sk-...',
    },
    grok: {
        img: '/grok-icon.svg',
        description: "Grok-3 and frontier reasoning models from Elon Musk's AI lab, xAI.",
        keyPlaceholder: 'xai-...',
        keyLink: { label: 'Get your key at xAI Console →', url: 'https://console.x.ai/' },
        keyNote: 'Key starts with xai-...',
    },
    deepseek: {
        img: '/deepseek-logo-icon.svg',
        description: 'DeepSeek-V3 (chat + tools) and DeepSeek-R1 (powerful chain-of-thought reasoning).',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at DeepSeek Platform →', url: 'https://platform.deepseek.com/api_keys' },
        keyNote: 'Note: deepseek-reasoner (R1) does not support tool/function calling.',
    },
    openai_compatible: {
        img: '/chatgpt-icon.svg',
        description: 'Any OpenAI v1-compatible cloud provider (OpenRouter, Together, Fireworks, etc.).',
        keyPlaceholder: 'sk-...',
        keyNote: 'Enter your API key, base URL, and model names below.',
    },
    local_compatible: {
        img: OLLAMA_IMG,
        description: 'Any local OpenAI v1-compatible server (vLLM, LM Studio, llama.cpp server, etc.).',
        keyNote: 'Enter your base URL and model names below. API key is optional.',
    },
    bedrock: {
        img: '/aws-bedrock-icon.svg',
        description: 'Enterprise-grade models via AWS, including Claude, Llama, and Titan.',
        keyPlaceholder: 'ABSK... or bedrock-api-key...',
        keyLink: { label: 'Set up Bedrock API keys in AWS Console →', url: 'https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-generate.html#api-keys-generate-console' },
        keyNote: 'Supports long-term keys (ABSK...) and temporary keys (bedrock-api-key...). Bearer prefix is auto-normalized.',
    },
    anthropic_cli: {
        img: '/claude-ai-icon.svg',
        description: 'Use the locally installed Anthropic Claude CLI. No API key needed — uses your existing terminal session.',
    },
    gemini_cli: {
        img: '/google-gemini-icon.svg',
        description: 'Use the locally installed Google Gemini CLI. No API key needed — uses your existing terminal session.',
    },
    codex_cli: {
        description: 'Use the locally installed OpenAI Codex CLI agent. No API key needed — uses your existing terminal session.',
    },
    github_copilot_cli: {
        description: 'Use the locally installed GitHub Copilot CLI. No API key needed — uses your existing GitHub session.',
    },
};

export const ModelsTab = ({
    providers, selectedModel, setSelectedModel,
    embeddingModel, setEmbeddingModel,
    builderModel, setBuilderModel,
    maxOutputTokens, setMaxOutputTokens,
    reasoningEffort, setReasoningEffort, reasoningWithoutOff, reasoningOffMinimum,
    openaiKey, setOpenaiKey, anthropicKey, setAnthropicKey,
    geminiKey, setGeminiKey, grokKey, setGrokKey,
    deepseekKey, setDeepseekKey,
    bedrockApiKey, setBedrockApiKey,
    awsRegion, setAwsRegion, loadingModels,
    onSave, isSaving, mode, setMode, localModels, cloudModels, filteredModels,
    openaiCompatibleKey, setOpenaiCompatibleKey,
    openaiCompatibleBaseUrl, setOpenaiCompatibleBaseUrl,
    openaiCompatibleModels, setOpenaiCompatibleModels,
    openaiCompatibleEmbedModels, setOpenaiCompatibleEmbedModels,
    localCompatibleBaseUrl, setLocalCompatibleBaseUrl,
    localCompatibleKey, setLocalCompatibleKey,
    localCompatibleModels, setLocalCompatibleModels,
    localCompatibleEmbedModels, setLocalCompatibleEmbedModels,
    huggingfaceToken, setHuggingfaceToken,
    huggingfaceModels, setHuggingfaceModels,
    anthropicCliModels, setAnthropicCliModels,
    geminiCliModels, setGeminiCliModels,
    codexCliModels, setCodexCliModels,
    githubCopilotCliModels, setGithubCopilotCliModels,
    knowledgeModels, setKnowledgeModels,
}: ModelsTabProps) => {

    const { secrets } = useSavedSecrets();
    const [expandedProvider, setExpandedProvider] = useState<string | null>(null);
    const [visibleKeys, setVisibleKeys] = useState<Record<string, boolean>>({});
    const toggleKeyVisible = (k: string) => setVisibleKeys(prev => ({ ...prev, [k]: !prev[k] }));

    // Build all available models list for default selector
    const allAvailable: string[] = [];
    Object.entries(providers).forEach(([, info]) => {
        if (info.available) allAvailable.push(...info.models);
    });

    const getKeyValue = (provider: string) => {
        switch (provider) {
            case 'openai': return openaiKey;
            case 'anthropic': return anthropicKey;
            case 'gemini': return geminiKey;
            case 'grok': return grokKey;
            case 'deepseek': return deepseekKey;
            case 'bedrock': return bedrockApiKey;
            case 'openai_compatible': return openaiCompatibleKey;
            case 'local_compatible': return localCompatibleKey;
            default: return '';
        }
    };

    const setKeyValue = (provider: string, value: string) => {
        switch (provider) {
            case 'openai': setOpenaiKey(value); break;
            case 'anthropic': setAnthropicKey(value); break;
            case 'gemini': setGeminiKey(value); break;
            case 'grok': setGrokKey(value); break;
            case 'deepseek': setDeepseekKey(value); break;
            case 'bedrock': setBedrockApiKey(value); break;
            case 'openai_compatible': setOpenaiCompatibleKey(value); break;
            case 'local_compatible': setLocalCompatibleKey(value); break;
        }
    };

    // Both lists, and the builder's picker, are built by one helper in
    // lib/models — they used to be three separate flatMaps that disagreed.
    const modelOptions = withUnreachable(buildModelOptions(providers), selectedModel);
    const embeddingOptions = withUnreachable(
        buildModelOptions(providers, 'embedding_models'), embeddingModel,
    );
    // Empty means "whatever the default model is", which is a real choice and
    // needs a row of its own — a blank control reads as nothing being set.
    const builderOptions = [
        { value: DEFAULT_SENTINEL, label: `Same as default model${selectedModel ? ` (${selectedModel})` : ''}` },
        ...withUnreachable(buildModelOptions(providers), builderModel),
    ];

    return (
        <div>
            {/* Provider Cards: each a separate object, so each keeps its border (UX1). */}
            <Section variant="plain" collapsible={false} title="Providers">
                <div className={`${FORM} space-y-2`}>
                    {Object.entries(PROVIDER_META).map(([key, meta]) => {
                        const providerData = providers[key] || { available: false, models: [] };
                        const isExpanded = expandedProvider === key;
                        const modelCount = providerData.models.length;

                        return (
                            <ProviderCard
                                key={key}
                                name={providerLabel(key)}
                                img={meta.img}
                                imgClassName={meta.img === OLLAMA_IMG ? 'theme-adaptive-icon' : undefined}
                                live={providerData.available}
                                status={providerData.available
                                    ? `${modelCount} model${modelCount !== 1 ? 's' : ''}`
                                    : key === 'ollama' ? 'Not running' : key.endsWith('_cli') ? 'Not installed' : 'No key configured'}
                                open={isExpanded}
                                onToggle={() => setExpandedProvider(isExpanded ? null : key)}
                            >
                                        <p className="text-xs text-text-muted">{meta.description}</p>

                                        {/* API Key input (not for Ollama, Bedrock, or CLI — they have their own blocks) */}
                                        {key !== 'ollama' && key !== 'bedrock' && key !== 'openai_compatible' && key !== 'local_compatible' && key !== 'huggingface' && !key.endsWith('_cli') && (
                                            <div className="space-y-1.5">
                                                <Label htmlFor={`api-key-${key}`} className={FIELD_LABEL}>API key</Label>
                                                <div className="relative">
                                                    <Input
                                                        id={`api-key-${key}`}
                                                        type={visibleKeys[key] ? 'text' : 'password'}
                                                        value={getKeyValue(key)}
                                                        onChange={e => setKeyValue(key, e.target.value)}
                                                        className="pr-8"
                                                        placeholder={secretPlaceholder(secrets, `${key}_key`, meta.keyPlaceholder ?? '')}
                                                    />
                                                    <button type="button" onClick={() => toggleKeyVisible(key)}
                                                        aria-label={visibleKeys[key] ? 'Hide API key' : 'Show API key'}
                                                        className="absolute right-2 top-1/2 -translate-y-1/2 text-text-faint hover:text-text transition-colors">
                                                        {visibleKeys[key] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                    </button>
                                                </div>
                                                <SavedSecret name={`${key}_key`} label={`${providerLabel(key)} API key`} typed={getKeyValue(key)} />
                                                {/* Key instructions */}
                                                {meta.keyNote && (
                                                    <p className="text-2xs text-text-faint">{meta.keyNote}</p>
                                                )}
                                                {meta.keyLink && (
                                                    <a
                                                        href={meta.keyLink.url}
                                                        target="_blank"
                                                        rel="noopener noreferrer"
                                                        className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline"
                                                    >
                                                        <ExternalLink className="h-2.5 w-2.5" />
                                                        {meta.keyLink.label}
                                                    </a>
                                                )}
                                            </div>
                                        )}

                                        {/* Bedrock-specific fields */}
                                        {key === 'bedrock' && (
                                            <div className="space-y-3">
                                                <div className="space-y-1.5">
                                                    <Label htmlFor="bedrockapikey" className={FIELD_LABEL}>Bedrock API key</Label>
                                                    <div className="relative">
                                                        <Input id="bedrockapikey" type={visibleKeys['bedrock'] ? 'text' : 'password'} value={bedrockApiKey} onChange={e => setBedrockApiKey(e.target.value)}
                                                            className="pr-8" placeholder={secretPlaceholder(secrets, 'bedrock_api_key', "ABSK... or bedrock-api-key...")} />
                                                        <button type="button" onClick={() => toggleKeyVisible('bedrock')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-faint hover:text-text transition-colors">
                                                            {visibleKeys['bedrock'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                    <SavedSecret name="bedrock_api_key" label="Bedrock API key" typed={bedrockApiKey} />
                                                    {meta.keyNote && (
                                                        <p className="text-2xs text-text-faint">{meta.keyNote}</p>
                                                    )}
                                                    {meta.keyLink && (
                                                        <a
                                                            href={meta.keyLink.url}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline"
                                                        >
                                                            <ExternalLink className="h-2.5 w-2.5" />
                                                            {meta.keyLink.label}
                                                        </a>
                                                    )}
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="awsregion" className={FIELD_LABEL}>AWS region</Label>
                                                    <Input id="awsregion" type="text" value={awsRegion} onChange={e => setAwsRegion(e.target.value)}
                                                         placeholder="us-east-1" />
                                                </div>
                                                {/* A profile is a model since batch 1B: discovery lists each as
                                                    `bedrock.<id>`, and the workspace-wide override this card used
                                                    to set is no longer applied. Its warning said Bedrock needed
                                                    one chosen, which sent people to the one setting that does
                                                    nothing (F66). */}
                                                <p className="text-2xs text-text-faint leading-relaxed">
                                                    Inference profiles are listed as models — the <span className="font-mono">us.</span>,{' '}
                                                    <span className="font-mono">eu.</span>, <span className="font-mono">apac.</span> and{' '}
                                                    <span className="font-mono">global.</span> variants and your application profiles. Pick one
                                                    wherever you choose a model; a model with no on-demand throughput is only available that way.
                                                </p>
                                            </div>
                                        )}

                                        {/* OpenAI Compatible fields */}
                                        {key === 'openai_compatible' && (
                                            <div className="space-y-3">
                                                <div className="space-y-1.5">
                                                    <Label htmlFor="openaicompatiblekey" className={FIELD_LABEL}>API key</Label>
                                                    <div className="relative">
                                                        <Input id="openaicompatiblekey" type={visibleKeys['openai_compatible'] ? 'text' : 'password'} value={openaiCompatibleKey} onChange={e => setOpenaiCompatibleKey(e.target.value)}
                                                            className="pr-8" placeholder={secretPlaceholder(secrets, 'openai_compatible_key', "sk-...")} />
                                                        <button type="button" onClick={() => toggleKeyVisible('openai_compatible')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-faint hover:text-text transition-colors">
                                                            {visibleKeys['openai_compatible'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                    <SavedSecret name="openai_compatible_key" label="OpenAI-compatible API key" typed={openaiCompatibleKey} />
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="openaicompatiblebaseurl" className={FIELD_LABEL}>Base URL</Label>
                                                    <Input id="openaicompatiblebaseurl" type="text" value={openaiCompatibleBaseUrl} onChange={e => setOpenaiCompatibleBaseUrl(e.target.value)}
                                                         placeholder="https://openrouter.ai/api" />
                                                    <p className="text-2xs text-text-faint">The /v1 path is appended automatically. Do not include /v1 in the URL.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="openaicompatiblemodels" className={FIELD_LABEL}>Model names (comma-separated)</Label>
                                                    <Input id="openaicompatiblemodels" type="text" value={openaiCompatibleModels} onChange={e => setOpenaiCompatibleModels(e.target.value)}
                                                         placeholder="e.g. meta-llama/llama-3-70b-instruct, google/gemma-2-27b-it" />
                                                    <p className="text-2xs text-text-faint">If the /v1/models endpoint is available, models will be fetched automatically. Otherwise, list them here.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="openaicompatibleembedmodels" className={FIELD_LABEL}>Embedding model names (comma-separated)</Label>
                                                    <Input id="openaicompatibleembedmodels" type="text" value={openaiCompatibleEmbedModels} onChange={e => setOpenaiCompatibleEmbedModels(e.target.value)}
                                                         placeholder="e.g. hf:nomic-ai/nomic-embed-text-v1.5" />
                                                    <p className="text-2xs text-text-faint">Models listed here appear in the embedding model dropdown. Models with &quot;embed&quot; in the name are also auto-detected from /v1/models.</p>
                                                </div>
                                            </div>
                                        )}

                                        {/* Local V1 Compatible fields */}
                                        {key === 'local_compatible' && (
                                            <div className="space-y-3">
                                                <div className="space-y-1.5">
                                                    <Label htmlFor="localcompatiblekey" className={FIELD_LABEL}>API key (optional)</Label>
                                                    <div className="relative">
                                                        <Input id="localcompatiblekey" type={visibleKeys['local_compatible'] ? 'text' : 'password'} value={localCompatibleKey} onChange={e => setLocalCompatibleKey(e.target.value)}
                                                            className="pr-8" placeholder={secretPlaceholder(secrets, 'local_compatible_key', "Leave blank if not required")} />
                                                        <button type="button" onClick={() => toggleKeyVisible('local_compatible')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-faint hover:text-text transition-colors">
                                                            {visibleKeys['local_compatible'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                    <SavedSecret name="local_compatible_key" label="local v1-compatible API key" typed={localCompatibleKey} />
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="localcompatiblebaseurl" className={FIELD_LABEL}>Base URL</Label>
                                                    <Input id="localcompatiblebaseurl" type="text" value={localCompatibleBaseUrl} onChange={e => setLocalCompatibleBaseUrl(e.target.value)}
                                                         placeholder="http://localhost:8000" />
                                                    <p className="text-2xs text-text-faint">The /v1 path is appended automatically. Do not include /v1 in the URL.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="localcompatiblemodels" className={FIELD_LABEL}>Model names (comma-separated)</Label>
                                                    <Input id="localcompatiblemodels" type="text" value={localCompatibleModels} onChange={e => setLocalCompatibleModels(e.target.value)}
                                                         placeholder="e.g. llama-3-70b, mistral-7b" />
                                                    <p className="text-2xs text-text-faint">If the /v1/models endpoint is available, models will be fetched automatically. Otherwise, list them here.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="localcompatibleembedmodels" className={FIELD_LABEL}>Embedding model names (comma-separated)</Label>
                                                    <Input id="localcompatibleembedmodels" type="text" value={localCompatibleEmbedModels} onChange={e => setLocalCompatibleEmbedModels(e.target.value)}
                                                         placeholder="e.g. bge-m3" />
                                                    <p className="text-2xs text-text-faint">Models listed here appear in the embedding model dropdown. Models with &quot;embed&quot; in the name are also auto-detected from /v1/models.</p>
                                                </div>
                                            </div>
                                        )}

                                        {/* HuggingFace fields */}
                                        {key === 'huggingface' && (
                                            <div className="space-y-3">
                                                <div className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5 text-xs leading-relaxed text-text">
                                                    <strong>Requires torch + transformers on the host.</strong> Models load in the backend process and stay in memory. Expect 16-40 GB VRAM for 7B-class models. Without a GPU, inference runs on CPU and will be slow.
                                                </div>
                                                <div className="space-y-1.5">
                                                    <Label htmlFor="huggingfacetoken" className={FIELD_LABEL}>Access token (optional)</Label>
                                                    <div className="relative">
                                                        <Input id="huggingfacetoken" type={visibleKeys['huggingface'] ? 'text' : 'password'} value={huggingfaceToken} onChange={e => setHuggingfaceToken(e.target.value)}
                                                            className="pr-8" placeholder={secretPlaceholder(secrets, 'huggingface_token', "hf_... (required for gated models like Llama, Gemma)")} />
                                                        <button type="button" onClick={() => toggleKeyVisible('huggingface')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-text-faint hover:text-text transition-colors">
                                                            {visibleKeys['huggingface'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                    <SavedSecret name="huggingface_token" label="Hugging Face token" typed={huggingfaceToken} />
                                                    <p className="text-2xs text-text-faint">Only needed for gated models. Public models load without a token.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="huggingfacemodels" className={FIELD_LABEL}>Model IDs (one per line or comma-separated)</Label>
                                                    <Textarea id="huggingfacemodels" value={huggingfaceModels} onChange={e => setHuggingfaceModels(e.target.value)} rows={4}
                                                        className="font-mono"
                                                        placeholder={"Qwen/Qwen2.5-7B-Instruct\nmeta-llama/Llama-3.1-8B-Instruct\nmistralai/Mistral-7B-Instruct-v0.3"}
                                                    />
                                                    <p className="text-2xs text-text-faint">Each ID becomes a selectable <code className="font-code text-text-muted">hf.&lt;org&gt;/&lt;model&gt;</code> model. First call to a model pays a 20-60s load cost, then stays warm in memory.</p>
                                                </div>
                                            </div>
                                        )}

                                        {/* Ollama info */}
                                        {key === 'ollama' && (
                                            <div className="space-y-1.5">
                                                <div className="text-2xs text-text-faint">
                                                    {providerData.available
                                                        ? `Detected ${modelCount} local model${modelCount !== 1 ? 's' : ''}: ${providerData.models.slice(0, 5).join(', ')}${modelCount > 5 ? '...' : ''}`
                                                        : 'Ollama is not running. Start it to use local models.'
                                                    }
                                                </div>
                                                <a
                                                    href="https://ollama.com/download"
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline"
                                                >
                                                    <ExternalLink className="h-2.5 w-2.5" />
                                                    Download Ollama →
                                                </a>
                                            </div>
                                        )}

                                        {/* CLI Sessions info */}
                                        {key.endsWith('_cli') && (
                                            <div className="space-y-2">
                                                <div className="text-2xs text-text-faint">
                                                    {providerData.available
                                                        ? `Detected ${modelCount} CLI session${modelCount !== 1 ? 's' : ''}: ${providerData.models.join(', ')}`
                                                        : `No CLI binary found in PATH for ${providerLabel(key)}.`
                                                    }
                                                </div>
                                                <div className="rounded-md bg-surface-2 px-3 py-2.5 text-xs leading-relaxed text-text-muted">
                                                    <strong>No API key needed</strong> — uses your existing CLI session. Run the CLI manually first to authenticate.
                                                </div>
                                                <div className="flex flex-col gap-1">
                                                    {key === 'anthropic_cli' && (
                                                        <a href="https://claude.ai/download" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install Claude CLI →
                                                        </a>
                                                    )}
                                                    {key === 'gemini_cli' && (
                                                        <a href="https://ai.google.dev/gemini-api/docs/gemini-cli" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install Gemini CLI →
                                                        </a>
                                                    )}
                                                    {key === 'codex_cli' && (
                                                        <a href="https://github.com/openai/codex" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install OpenAI Codex CLI →
                                                        </a>
                                                    )}
                                                    {key === 'github_copilot_cli' && (
                                                        <a href="https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-xs text-accent-ink hover:underline">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install GitHub Copilot CLI →
                                                        </a>
                                                    )}
                                                </div>
                                                {(() => {
                                                    const cliCustom: Record<string, { value: string; set: (v: string) => void; placeholder: string; prefix: string }> = {
                                                        anthropic_cli: { value: anthropicCliModels, set: setAnthropicCliModels, placeholder: 'e.g. claude-opus-4-5-20251101', prefix: 'cli.claude' },
                                                        gemini_cli: { value: geminiCliModels, set: setGeminiCliModels, placeholder: 'e.g. gemini-2.5-pro', prefix: 'cli.gemini' },
                                                        codex_cli: { value: codexCliModels, set: setCodexCliModels, placeholder: 'e.g. gpt-5.4, gpt-5.4-mini', prefix: 'cli.codex' },
                                                        github_copilot_cli: { value: githubCopilotCliModels, set: setGithubCopilotCliModels, placeholder: 'e.g. claude-sonnet-4-5, gpt-4.1', prefix: 'cli.copilot' },
                                                    };
                                                    const cfg = cliCustom[key];
                                                    if (!cfg) return null;
                                                    return (
                                                        <div className="space-y-1">
                                                            <Label htmlFor="cfg-value" className={FIELD_LABEL}>Custom model names (comma-separated)</Label>
                                                            <Input id="cfg-value" type="text" value={cfg.value} onChange={e => cfg.set(e.target.value)}
                                                                 placeholder={cfg.placeholder} />
                                                            <p className="text-2xs text-text-faint">Each name is passed to the CLI's <code className="font-code text-text-muted">-m</code> flag and becomes a selectable <code className="font-code text-text-muted">{cfg.prefix}.&lt;name&gt;</code> model. Use this when the CLI's default model isn't available to your account.</p>
                                                        </div>
                                                    );
                                                })()}
                                            </div>
                                        )}

                                        {/* Available models list */}
                                        {providerData.available && providerData.models.length > 0 && key !== 'ollama' && (
                                            <div className="space-y-1">
                                                <Label className={FIELD_LABEL}>Available models</Label>
                                                <div className="flex flex-wrap gap-1.5">
                                                    {providerData.models.map(m => (
                                                        <span key={m} className="rounded-sm border border-border bg-surface-2 px-2 py-0.5 text-2xs text-text-muted">{m}</span>
                                                    ))}
                                                </div>
                                            </div>
                                        )}
                            </ProviderCard>
                        );
                    })}
                </div>
            </Section>

            {/* Default Model Selector */}
            <Section variant="plain" collapsible={false} title="Default model">
              <div className={`${FORM} space-y-2`}>
                <p className="text-sm text-text-muted">Used for system prompt generation and agents without a specific model assigned.</p>
                <Combobox
                    value={selectedModel || undefined}
                    onChange={setSelectedModel}
                    options={modelOptions}
                    placeholder={loadingModels ? 'Loading models…' : 'Select default model…'}
                    searchPlaceholder="Search models…"
                    aria-label="Default model"
                />
              </div>
            </Section>
            {/* Reasoning effort — the workspace default (F269). Every agent with
                none of its own runs at it; nothing on any screen set it. All four
                levels stay: it reaches every model, not only the default one. */}
            <Section variant="plain" collapsible={false} title="Reasoning effort">
              <div className={`${FORM} space-y-2`}>
                <p id="models-effort-help" className="text-sm text-text-muted">
                    How much a model that can think does before it answers, for every agent that sets none of
                    its own. Higher costs more and takes longer; thinking tokens are billed as output.
                </p>
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
                    <p className="text-2xs text-text-muted">{selectedModel} can&apos;t stop thinking entirely: Off runs at its lowest thinking level.</p>
                )}
                {reasoningWithoutOff.includes(selectedModel) && (
                    <p className="text-2xs text-text-muted">{selectedModel} has no Off: the least it thinks is Low.</p>
                )}
              </div>
            </Section>
            {/* Max response length (F263). The "raise the response cap" error
                pointed here while there was nothing here to raise. */}
            <Section variant="plain" collapsible={false} title="Max response length">
              <div className={`${FORM} space-y-2`}>
                <p id="models-max-output-help" className="text-sm text-text-muted">
                    The most a model may write in one response, thinking included, in tokens. Blank is automatic:
                    8,192 for most providers and no limit for OpenAI, OpenAI-compatible and local servers. A model
                    that thinks always gets room past its thinking. An agent can set its own.
                </p>
                <Input id="models-max-output"
                    type="number"
                    inputMode="numeric"
                    min={256}
                    max={262144}
                    step={1024}
                    placeholder="Automatic"
                    value={maxOutputTokens}
                    onChange={e => setMaxOutputTokens(e.target.value)}
                    aria-describedby="models-max-output-help"
                    aria-label="Max response length"
                    className="w-48 tabular-nums"
                />
              </div>
            </Section>
            {/* Builder Model Selector.

                `builder_model` has been writable and honoured end to end since
                the builder shipped, and nothing here ever set it — the only way
                to change it was the "Set as default" button inside the panel,
                which you have to already be in the panel to find. */}
            <Section variant="plain" collapsible={false} title="Builder model">
              <div className={`${FORM} space-y-2`}>
                <p className="text-sm text-text-muted">Used by the AI builder for every step of a build. Reasoning-capable models do noticeably better here.</p>
                <Combobox
                    value={builderModel || DEFAULT_SENTINEL}
                    onChange={(v) => setBuilderModel(v === DEFAULT_SENTINEL ? '' : v)}
                    options={builderOptions}
                    placeholder={loadingModels ? 'Loading models…' : 'Select builder model…'}
                    searchPlaceholder="Search models…"
                    aria-label="Builder model"
                />
              </div>
            </Section>
            {/* Default Embedding Model Selector */}
            <Section variant="plain" collapsible={false} title="Embedding model">
              <div className={`${FORM} space-y-3`}>
                <div className="space-y-2">
                    <p className="text-sm text-text-muted">Indexes code repositories, matches each workflow step&apos;s semantic cache, and embeds knowledge bases unless Knowledge has its own. Gemini, OpenAI, Bedrock, Ollama and OpenAI-compatible servers embed.</p>
                    <Combobox
                        value={embeddingModel || undefined}
                        onChange={setEmbeddingModel}
                        options={embeddingOptions}
                        placeholder={loadingModels ? 'Loading models…' : 'Select default embedding model…'}
                        searchPlaceholder="Search embedding models…"
                        aria-label="Embedding model"
                    />
                </div>
                
                {/* Warning Message */}
                <div className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5">
                    <p className="text-xs leading-relaxed text-text">
                        <strong className="font-semibold">Warning:</strong>{' '}Changing the global embedding model will affect new repository indexes.
                        Existing repositories will NOT be automatically migrated. Use individual repo settings to re-index manually if needed.
                        Each step&apos;s semantic cache starts afresh.
                    </p>
                </div>
              </div>
            </Section>

            <KnowledgeModels value={knowledgeModels} onChange={setKnowledgeModels} providers={providers}
                embeddingModel={embeddingModel} chatModel={selectedModel} />

            <SaveBar onSave={onSave} isSaving={isSaving} />
        </div>
    );
};
