import { Check, X as XIcon, ChevronDown, ChevronUp, ExternalLink, Terminal, Eye, EyeOff } from 'lucide-react';
import { Combobox, Input, Label, Spinner, Textarea } from '@/components/ui';
import { modelOptions as buildModelOptions, providerLabel, withUnreachable } from '@/lib/models';

/** `builder_model` is empty when it should follow `model`; a Combobox needs a value. */
const DEFAULT_SENTINEL = '__default__';
import React, { useState } from 'react';
import { SavedSecret, secretPlaceholder, useSavedSecrets } from './SavedSecret';

type BrandIconProps = { className?: string; style?: React.CSSProperties };

// Every brand mark below is decoration: it sits beside the provider's own name,
// so an `alt` would make a screen reader read that name twice — and the Local V1
// Compatible card borrows Ollama's mark, so it read out the wrong name entirely.
// Keep these silent; the visible label is what names the card (F44).

const OllamaIcon = ({ className }: BrandIconProps) => (
    <img src="/ollama-icon.svg" className={`${className} theme-adaptive-icon`} alt="" />
);

const GeminiIcon = ({ className }: BrandIconProps) => (
    <img src="/google-gemini-icon.svg" className={className} alt="" />
);

const AnthropicIcon = ({ className }: BrandIconProps) => (
    <img src="/claude-ai-icon.svg" className={className} alt="" />
);

const OpenAIIcon = ({ className }: BrandIconProps) => (
    <img src="/chatgpt-icon.svg" className={className} alt="" />
);

const AWSIcon = ({ className }: BrandIconProps) => (
    <img src="/aws-bedrock-icon.svg" className={className} alt="" />
);

// xAI Grok — inline SVG X lettermark matching xAI brand
const GrokIcon = ({ className }: BrandIconProps) => (
    <img src="/grok-icon.svg" className={className} alt="" />
);

// DeepSeek — inline SVG whale/wave mark
const DeepSeekIcon = ({ className }: BrandIconProps) => (
    <img src="/deepseek-logo-icon.svg" className={className} alt="" />
);

// CLI Sessions — terminal icon from lucide
const CliIcon = ({ className }: BrandIconProps) => (
    <Terminal className={className} />
);

// HuggingFace — yellow circle emoji-mark fallback (no external SVG dependency)
const HuggingFaceIcon = ({ className, style }: BrandIconProps) => (
    <span className={className} style={{ ...style, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.9em', lineHeight: 1 }} aria-hidden="true">🤗</span>
);

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
    // Backward compat
    mode: string; setMode: (v: string) => void;
    localModels: string[]; cloudModels: string[];
    filteredModels: string[];
}

interface ProviderMeta {
    icon: React.FC<BrandIconProps>;
    color: string;
    description: string;
    keyPlaceholder?: string;
    /** Short link label and URL for getting an API key */
    keyLink?: { label: string; url: string };
    /** Extra human-readable note about the key */
    keyNote?: string;
}

const PROVIDER_META: Record<string, ProviderMeta> = {
    ollama: {
        icon: OllamaIcon,
        color: '#22c55e',
        description: 'Runs locally on your machine. Private, free, no API key needed.',
    },
    huggingface: {
        icon: HuggingFaceIcon,
        color: '#facc15',
        description: 'Run HuggingFace transformers models directly on the host machine. Requires torch + transformers installed and (for large models) a GPU.',
        keyPlaceholder: 'hf_...',
        keyLink: { label: 'Get a token at HuggingFace →', url: 'https://huggingface.co/settings/tokens' },
        keyNote: 'Token is only needed for gated models (Llama, Gemma, etc.). Public models work without it.',
    },
    gemini: {
        icon: GeminiIcon,
        color: '#4285f4',
        description: 'Google AI models. Fast and capable, with a generous free tier.',
        keyPlaceholder: 'AIza...',
        keyLink: { label: 'Get a free key at Google AI Studio →', url: 'https://aistudio.google.com/app/apikey' },
        keyNote: 'Free tier available. Key starts with AIza...',
    },
    anthropic: {
        icon: AnthropicIcon,
        color: '#d97706',
        description: 'Advanced reasoning and analysis via Claude models.',
        keyPlaceholder: 'sk-ant-...',
        keyLink: { label: 'Get your key at Anthropic Console →', url: 'https://console.anthropic.com/settings/keys' },
        keyNote: 'Key starts with sk-ant-api03-...',
    },
    openai: {
        icon: OpenAIIcon,
        color: '#10b981',
        description: 'GPT-4o and latest OpenAI models.',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at OpenAI Platform →', url: 'https://platform.openai.com/api-keys' },
        keyNote: 'Key starts with sk-proj-... or sk-...',
    },
    grok: {
        icon: GrokIcon,
        color: '#e5e7eb',
        description: "Grok-3 and frontier reasoning models from Elon Musk's AI lab, xAI.",
        keyPlaceholder: 'xai-...',
        keyLink: { label: 'Get your key at xAI Console →', url: 'https://console.x.ai/' },
        keyNote: 'Key starts with xai-...',
    },
    deepseek: {
        icon: DeepSeekIcon,
        color: '#4f6ef7',
        description: 'DeepSeek-V3 (chat + tools) and DeepSeek-R1 (powerful chain-of-thought reasoning).',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at DeepSeek Platform →', url: 'https://platform.deepseek.com/api_keys' },
        keyNote: 'Note: deepseek-reasoner (R1) does not support tool/function calling.',
    },
    openai_compatible: {
        icon: OpenAIIcon,
        color: '#f97316',
        description: 'Any OpenAI v1-compatible cloud provider (OpenRouter, Together, Fireworks, etc.).',
        keyPlaceholder: 'sk-...',
        keyNote: 'Enter your API key, base URL, and model names below.',
    },
    local_compatible: {
        icon: OllamaIcon,
        color: '#06b6d4',
        description: 'Any local OpenAI v1-compatible server (vLLM, LM Studio, llama.cpp server, etc.).',
        keyNote: 'Enter your base URL and model names below. API key is optional.',
    },
    bedrock: {
        icon: AWSIcon,
        color: '#f59e0b',
        description: 'Enterprise-grade models via AWS, including Claude, Llama, and Titan.',
        keyPlaceholder: 'ABSK... or bedrock-api-key...',
        keyLink: { label: 'Set up Bedrock API keys in AWS Console →', url: 'https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-generate.html#api-keys-generate-console' },
        keyNote: 'Supports long-term keys (ABSK...) and temporary keys (bedrock-api-key...). Bearer prefix is auto-normalized.',
    },
    anthropic_cli: {
        icon: AnthropicIcon,
        color: '#d97706',
        description: 'Use the locally installed Anthropic Claude CLI. No API key needed — uses your existing terminal session.',
    },
    gemini_cli: {
        icon: GeminiIcon,
        color: '#4285f4',
        description: 'Use the locally installed Google Gemini CLI. No API key needed — uses your existing terminal session.',
    },
    codex_cli: {
        icon: CliIcon,
        color: '#a78bfa',
        description: 'Use the locally installed OpenAI Codex CLI agent. No API key needed — uses your existing terminal session.',
    },
    github_copilot_cli: {
        icon: CliIcon,
        color: '#8957e5',
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
        <div className="space-y-8">
            {/* Provider Cards */}
            <div className="space-y-4">
                <Label size="sm" className="block">Providers</Label>
                <div className="space-y-3">
                    {Object.entries(PROVIDER_META).map(([key, meta]) => {
                        const providerData = providers[key] || { available: false, models: [] };
                        const isExpanded = expandedProvider === key;
                        const Icon = meta.icon;
                        const modelCount = providerData.models.length;

                        return (
                            <div key={key} className={`border transition-all duration-200 ${providerData.available
                                ? 'border-border-strong bg-surface/50'
                                : 'border-border/50 bg-bg'
                                } rounded-md`}>
                                {/* Card Header */}
                                <button
                                    onClick={() => {
                                        const next = isExpanded ? null : key;
                                        setExpandedProvider(next);
                                    }}
                                    className="w-full flex items-center justify-between p-4 text-left hover:bg-surface/30 transition-colors"
                                >
                                    <div className="flex items-center gap-3">
                                        <div className={`h-2 w-2 rounded-full ${providerData.available ? 'bg-success shadow-[0_0_6px_var(--success)]' : 'bg-text-faint'}`} />
                                        <Icon className={`h-4 w-4 ${!providerData.available ? 'opacity-40 grayscale' : ''}`} style={{ color: providerData.available ? meta.color : '#71717a' }} />
                                        <div>
                                            <span className={`text-sm font-bold ${providerData.available ? 'text-text' : 'text-text-faint'}`}>
                                                {providerLabel(key)}
                                            </span>{' '}
                                            <span className="text-2xs text-text-faint ml-2">
                                                {providerData.available
                                                    ? `${modelCount} model${modelCount !== 1 ? 's' : ''}`
                                                    : key === 'ollama' ? 'Not running' : key.endsWith('_cli') ? 'Not installed' : 'No key configured'
                                                }
                                            </span>
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        {providerData.available
                                            ? <Check className="h-4 w-4 text-success" />
                                            : <XIcon className="h-3 w-3 text-text-faint" />
                                        }
                                        {isExpanded ? <ChevronUp className="h-4 w-4 text-text-faint" /> : <ChevronDown className="h-4 w-4 text-text-faint" />}
                                    </div>
                                </button>

                                {/* Expanded Content */}
                                {isExpanded && (
                                    <div className="px-4 pb-4 space-y-3 border-t border-border/50 pt-3">
                                        <p className="text-2xs text-text-faint">{meta.description}</p>

                                        {/* API Key input (not for Ollama, Bedrock, or CLI — they have their own blocks) */}
                                        {key !== 'ollama' && key !== 'bedrock' && key !== 'openai_compatible' && key !== 'local_compatible' && key !== 'huggingface' && !key.endsWith('_cli') && (
                                            <div className="space-y-1.5">
                                                <Label htmlFor={`api-key-${key}`} className="block">API Key</Label>
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
                                                        className="inline-flex items-center gap-1 text-2xs text-accent hover:text-accent-hover transition-colors"
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
                                                    <Label htmlFor="bedrockapikey" className="block">Bedrock API Key</Label>
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
                                                            className="inline-flex items-center gap-1 text-2xs text-accent hover:text-accent-hover transition-colors"
                                                        >
                                                            <ExternalLink className="h-2.5 w-2.5" />
                                                            {meta.keyLink.label}
                                                        </a>
                                                    )}
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="awsregion" className="block">AWS Region</Label>
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
                                                    <Label htmlFor="openaicompatiblekey" className="block">API Key</Label>
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
                                                    <Label htmlFor="openaicompatiblebaseurl" className="block">Base URL</Label>
                                                    <Input id="openaicompatiblebaseurl" type="text" value={openaiCompatibleBaseUrl} onChange={e => setOpenaiCompatibleBaseUrl(e.target.value)}
                                                         placeholder="https://openrouter.ai/api" />
                                                    <p className="text-2xs text-text-faint">The /v1 path is appended automatically. Do not include /v1 in the URL.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="openaicompatiblemodels" className="block">Model Names (comma-separated)</Label>
                                                    <Input id="openaicompatiblemodels" type="text" value={openaiCompatibleModels} onChange={e => setOpenaiCompatibleModels(e.target.value)}
                                                         placeholder="e.g. meta-llama/llama-3-70b-instruct, google/gemma-2-27b-it" />
                                                    <p className="text-2xs text-text-faint">If the /v1/models endpoint is available, models will be fetched automatically. Otherwise, list them here.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="openaicompatibleembedmodels" className="block">Embedding Model Names (comma-separated)</Label>
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
                                                    <Label htmlFor="localcompatiblekey" className="block">API Key (optional)</Label>
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
                                                    <Label htmlFor="localcompatiblebaseurl" className="block">Base URL</Label>
                                                    <Input id="localcompatiblebaseurl" type="text" value={localCompatibleBaseUrl} onChange={e => setLocalCompatibleBaseUrl(e.target.value)}
                                                         placeholder="http://localhost:8000" />
                                                    <p className="text-2xs text-text-faint">The /v1 path is appended automatically. Do not include /v1 in the URL.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="localcompatiblemodels" className="block">Model Names (comma-separated)</Label>
                                                    <Input id="localcompatiblemodels" type="text" value={localCompatibleModels} onChange={e => setLocalCompatibleModels(e.target.value)}
                                                         placeholder="e.g. llama-3-70b, mistral-7b" />
                                                    <p className="text-2xs text-text-faint">If the /v1/models endpoint is available, models will be fetched automatically. Otherwise, list them here.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <Label htmlFor="localcompatibleembedmodels" className="block">Embedding Model Names (comma-separated)</Label>
                                                    <Input id="localcompatibleembedmodels" type="text" value={localCompatibleEmbedModels} onChange={e => setLocalCompatibleEmbedModels(e.target.value)}
                                                         placeholder="e.g. bge-m3" />
                                                    <p className="text-2xs text-text-faint">Models listed here appear in the embedding model dropdown. Models with &quot;embed&quot; in the name are also auto-detected from /v1/models.</p>
                                                </div>
                                            </div>
                                        )}

                                        {/* HuggingFace fields */}
                                        {key === 'huggingface' && (
                                            <div className="space-y-3">
                                                <div className="p-2.5 bg-warning/5 border border-warning/20 text-2xs text-warning leading-relaxed rounded-md">
                                                    <strong>Requires torch + transformers on the host.</strong> Models load in the backend process and stay in memory. Expect 16-40 GB VRAM for 7B-class models. Without a GPU, inference runs on CPU and will be slow.
                                                </div>
                                                <div className="space-y-1.5">
                                                    <Label htmlFor="huggingfacetoken" className="block">Access Token (optional)</Label>
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
                                                    <Label htmlFor="huggingfacemodels" className="block">Model IDs (one per line or comma-separated)</Label>
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
                                                    className="inline-flex items-center gap-1 text-2xs text-accent hover:text-accent-hover transition-colors"
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
                                                <div className="p-2.5 bg-accent/5 border border-accent/20 text-2xs text-accent leading-relaxed rounded-md">
                                                    <strong>No API key needed</strong> — uses your existing CLI session. Run the CLI manually first to authenticate.
                                                </div>
                                                <div className="flex flex-col gap-1">
                                                    {key === 'anthropic_cli' && (
                                                        <a href="https://claude.ai/download" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-2xs text-accent hover:text-accent-hover transition-colors">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install Claude CLI →
                                                        </a>
                                                    )}
                                                    {key === 'gemini_cli' && (
                                                        <a href="https://ai.google.dev/gemini-api/docs/gemini-cli" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-2xs text-accent hover:text-accent-hover transition-colors">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install Gemini CLI →
                                                        </a>
                                                    )}
                                                    {key === 'codex_cli' && (
                                                        <a href="https://github.com/openai/codex" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-2xs text-accent hover:text-accent-hover transition-colors">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install OpenAI Codex CLI →
                                                        </a>
                                                    )}
                                                    {key === 'github_copilot_cli' && (
                                                        <a href="https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-2xs text-accent hover:text-accent-hover transition-colors">
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
                                                            <Label htmlFor="cfg-value" className="block">Custom Model Names (comma-separated)</Label>
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
                                                <Label className="block">Available Models</Label>
                                                <div className="flex flex-wrap gap-1.5">
                                                    {providerData.models.map(m => (
                                                        <span key={m} className="text-2xs px-2 py-0.5 bg-surface-2 text-text-muted border border-border-strong/50 rounded-sm">{m}</span>
                                                    ))}
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        );
                    })}
                </div>
            </div>

            {/* Default Model Selector */}
            <div className="space-y-2">
                <Label size="sm" className="block">Default Model</Label>
                <p className="text-2xs text-text-faint -mt-1">Used for system prompt generation and agents without a specific model assigned.</p>
                <Combobox
                    value={selectedModel || undefined}
                    onChange={setSelectedModel}
                    options={modelOptions}
                    placeholder={loadingModels ? 'Loading models…' : 'Select default model…'}
                    searchPlaceholder="Search models…"
                    aria-label="Default model"
                />
            </div>
            {/* Reasoning effort — the workspace default (F269). Every agent with
                none of its own runs at it; nothing on any screen set it. All four
                levels stay: it reaches every model, not only the default one. */}
            <div className="space-y-2">
                <Label size="sm" className="block">Reasoning Effort</Label>
                <p id="models-effort-help" className="text-2xs text-text-faint -mt-1">
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
                                className={`flex-1 border p-2 text-2xs uppercase tracking-wide rounded-md transition-colors ${
                                    active
                                        ? 'border-accent bg-accent/10 text-accent'
                                        : 'border-border bg-bg text-text-muted hover:border-border-strong hover:text-text'
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
            {/* Max response length (F263). The "raise the response cap" error
                pointed here while there was nothing here to raise. */}
            <div className="space-y-2">
                <Label htmlFor="models-max-output" size="sm" className="block">Max Response Length</Label>
                <p id="models-max-output-help" className="text-2xs text-text-faint -mt-1">
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
                />
            </div>
            {/* Builder Model Selector.

                `builder_model` has been writable and honoured end to end since
                the builder shipped, and nothing here ever set it — the only way
                to change it was the "Set as default" button inside the panel,
                which you have to already be in the panel to find. */}
            <div className="space-y-2">
                <Label size="sm" className="block">Builder Model</Label>
                <p className="text-2xs text-text-faint -mt-1">Used by the AI builder for every step of a build. Reasoning-capable models do noticeably better here.</p>
                <Combobox
                    value={builderModel || DEFAULT_SENTINEL}
                    onChange={(v) => setBuilderModel(v === DEFAULT_SENTINEL ? '' : v)}
                    options={builderOptions}
                    placeholder={loadingModels ? 'Loading models…' : 'Select builder model…'}
                    searchPlaceholder="Search models…"
                    aria-label="Builder model"
                />
            </div>
            {/* Default Embedding Model Selector */}
            <div className="space-y-4">
                <div className="space-y-2">
                    <Label size="sm" className="block">Embedding Model</Label>
                    <p className="text-2xs text-text-faint -mt-1">Used for code indexing and repository search. Requires compatible providers like Gemini, OpenAI, or Ollama.</p>
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
                <div className="p-3 bg-warning-subtle border border-warning/50 rounded-sm">
                    <p className="text-2xs text-warning leading-relaxed uppercase font-bold tracking-tight">
                        ⚠ Warning: Changing the global embedding model will affect new repository indexals. 
                        Existing repositories will NOT be automatically migrated. Use individual repo settings to re-index manually if needed.
                    </p>
                </div>
            </div>

            <div className="pt-4 flex justify-end">
                <button
                    onClick={onSave}
                    disabled={isSaving}
                    className="flex items-center gap-2 px-6 py-2.5 text-sm font-bold bg-accent text-accent-fg hover:bg-accent-hover transition-all shadow-lg disabled:opacity-50 disabled:cursor-not-allowed rounded-md"
                >
                    {isSaving && <Spinner size={16} />}
                    {isSaving ? 'Saving…' : 'Save Changes'}
                </button>
            </div>
        </div>
    );
};
