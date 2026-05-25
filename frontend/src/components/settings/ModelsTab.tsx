/* eslint-disable @typescript-eslint/no-explicit-any */
import { Check, X as XIcon, ChevronDown, ChevronUp, ExternalLink, Info, Loader2, Terminal, Eye, EyeOff } from 'lucide-react';
import React, { useState } from 'react';

type BrandIconProps = { className?: string; style?: React.CSSProperties };

const OllamaIcon = ({ className }: BrandIconProps) => (
    <img src="/ollama-icon.svg" className={`${className} theme-adaptive-icon`} alt="Ollama" />
);

const GeminiIcon = ({ className }: BrandIconProps) => (
    <img src="/google-gemini-icon.svg" className={className} alt="Google Gemini" />
);

const AnthropicIcon = ({ className }: BrandIconProps) => (
    <img src="/claude-ai-icon.svg" className={className} alt="Anthropic Claude" />
);

const OpenAIIcon = ({ className }: BrandIconProps) => (
    <img src="/chatgpt-icon.svg" className={className} alt="OpenAI" />
);

const AWSIcon = ({ className }: BrandIconProps) => (
    <img src="/aws-bedrock-icon.svg" className={className} alt="AWS Bedrock" />
);

// xAI Grok — inline SVG X lettermark matching xAI brand
const GrokIcon = ({ className }: BrandIconProps) => (
    <img src="/grok-icon.svg" className={className} alt="Grok" />
);

// DeepSeek — inline SVG whale/wave mark
const DeepSeekIcon = ({ className }: BrandIconProps) => (
    <img src="/deepseek-logo-icon.svg" className={className} alt="DeepSeek" />
);

// CLI Sessions — terminal icon from lucide
const CliIcon = ({ className }: BrandIconProps) => (
    <Terminal className={className} />
);

// HuggingFace — yellow circle emoji-mark fallback (no external SVG dependency)
const HuggingFaceIcon = ({ className, style }: BrandIconProps) => (
    <span className={className} style={{ ...style, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: '0.9em', lineHeight: 1 }} aria-label="HuggingFace">🤗</span>
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
    openaiKey: string; setOpenaiKey: (v: string) => void;
    anthropicKey: string; setAnthropicKey: (v: string) => void;
    geminiKey: string; setGeminiKey: (v: string) => void;
    grokKey: string; setGrokKey: (v: string) => void;
    deepseekKey: string; setDeepseekKey: (v: string) => void;
    bedrockApiKey: string; setBedrockApiKey: (v: string) => void;
    awsRegion: string; setAwsRegion: (v: string) => void;
    bedrockInferenceProfile: string; setBedrockInferenceProfile: (v: string) => void;
    bedrockInferenceProfiles: any[];
    loadingInferenceProfiles: boolean;
    inferenceProfilesError?: string | null;
    loadingModels: boolean;
    onExpandBedrock?: () => void;
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
    // Backward compat
    mode: string; setMode: (v: string) => void;
    localModels: string[]; cloudModels: string[];
    filteredModels: string[];
}

interface ProviderMeta {
    label: string;
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
        label: 'Ollama (Local)',
        icon: OllamaIcon,
        color: '#22c55e',
        description: 'Runs locally on your machine. Private, free, no API key needed.',
    },
    huggingface: {
        label: 'HuggingFace (Local)',
        icon: HuggingFaceIcon,
        color: '#facc15',
        description: 'Run HuggingFace transformers models directly on the host machine. Requires torch + transformers installed and (for large models) a GPU.',
        keyPlaceholder: 'hf_...',
        keyLink: { label: 'Get a token at HuggingFace →', url: 'https://huggingface.co/settings/tokens' },
        keyNote: 'Token is only needed for gated models (Llama, Gemma, etc.). Public models work without it.',
    },
    gemini: {
        label: 'Google Gemini',
        icon: GeminiIcon,
        color: '#4285f4',
        description: 'Google AI models. Fast and capable, with a generous free tier.',
        keyPlaceholder: 'AIza...',
        keyLink: { label: 'Get a free key at Google AI Studio →', url: 'https://aistudio.google.com/app/apikey' },
        keyNote: 'Free tier available. Key starts with AIza...',
    },
    anthropic: {
        label: 'Anthropic Claude',
        icon: AnthropicIcon,
        color: '#d97706',
        description: 'Advanced reasoning and analysis via Claude models.',
        keyPlaceholder: 'sk-ant-...',
        keyLink: { label: 'Get your key at Anthropic Console →', url: 'https://console.anthropic.com/settings/keys' },
        keyNote: 'Key starts with sk-ant-api03-...',
    },
    openai: {
        label: 'OpenAI',
        icon: OpenAIIcon,
        color: '#10b981',
        description: 'GPT-4o and latest OpenAI models.',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at OpenAI Platform →', url: 'https://platform.openai.com/api-keys' },
        keyNote: 'Key starts with sk-proj-... or sk-...',
    },
    grok: {
        label: 'xAI Grok',
        icon: GrokIcon,
        color: '#e5e7eb',
        description: "Grok-3 and frontier reasoning models from Elon Musk's AI lab, xAI.",
        keyPlaceholder: 'xai-...',
        keyLink: { label: 'Get your key at xAI Console →', url: 'https://console.x.ai/' },
        keyNote: 'Key starts with xai-...',
    },
    deepseek: {
        label: 'DeepSeek',
        icon: DeepSeekIcon,
        color: '#4f6ef7',
        description: 'DeepSeek-V3 (chat + tools) and DeepSeek-R1 (powerful chain-of-thought reasoning).',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at DeepSeek Platform →', url: 'https://platform.deepseek.com/api_keys' },
        keyNote: 'Note: deepseek-reasoner (R1) does not support tool/function calling.',
    },
    openai_compatible: {
        label: 'OpenAI Compatible',
        icon: OpenAIIcon,
        color: '#f97316',
        description: 'Any OpenAI v1-compatible cloud provider (OpenRouter, Together, Fireworks, etc.).',
        keyPlaceholder: 'sk-...',
        keyNote: 'Enter your API key, base URL, and model names below.',
    },
    local_compatible: {
        label: 'Local V1 Compatible',
        icon: OllamaIcon,
        color: '#06b6d4',
        description: 'Any local OpenAI v1-compatible server (vLLM, LM Studio, llama.cpp server, etc.).',
        keyNote: 'Enter your base URL and model names below. API key is optional.',
    },
    bedrock: {
        label: 'AWS Bedrock',
        icon: AWSIcon,
        color: '#f59e0b',
        description: 'Enterprise-grade models via AWS, including Claude, Llama, and Titan.',
        keyPlaceholder: 'ABSK... or bedrock-api-key...',
        keyLink: { label: 'Set up Bedrock API keys in AWS Console →', url: 'https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-generate.html#api-keys-generate-console' },
        keyNote: 'Supports long-term keys (ABSK...) and temporary keys (bedrock-api-key...). Bearer prefix is auto-normalized.',
    },
    anthropic_cli: {
        label: 'Claude (CLI)',
        icon: AnthropicIcon,
        color: '#d97706',
        description: 'Use the locally installed Anthropic Claude CLI. No API key needed — uses your existing terminal session.',
    },
    gemini_cli: {
        label: 'Gemini (CLI)',
        icon: GeminiIcon,
        color: '#4285f4',
        description: 'Use the locally installed Google Gemini CLI. No API key needed — uses your existing terminal session.',
    },
    codex_cli: {
        label: 'Codex (CLI)',
        icon: CliIcon,
        color: '#a78bfa',
        description: 'Use the locally installed OpenAI Codex CLI agent. No API key needed — uses your existing terminal session.',
    },
    github_copilot_cli: {
        label: 'GitHub Copilot (CLI)',
        icon: CliIcon,
        color: '#8957e5',
        description: 'Use the locally installed GitHub Copilot CLI. No API key needed — uses your existing GitHub session.',
    },
};

export const ModelsTab = ({
    providers, selectedModel, setSelectedModel,
    embeddingModel, setEmbeddingModel,
    openaiKey, setOpenaiKey, anthropicKey, setAnthropicKey,
    geminiKey, setGeminiKey, grokKey, setGrokKey,
    deepseekKey, setDeepseekKey,
    bedrockApiKey, setBedrockApiKey,
    awsRegion, setAwsRegion, bedrockInferenceProfile, setBedrockInferenceProfile,
    bedrockInferenceProfiles, loadingInferenceProfiles, inferenceProfilesError, loadingModels,
    onExpandBedrock, onSave, isSaving, mode, setMode, localModels, cloudModels, filteredModels,
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
}: ModelsTabProps) => {
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

    return (
        <div className="space-y-8">
            {/* Provider Cards */}
            <div className="space-y-4">
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Providers</label>
                <div className="space-y-3">
                    {Object.entries(PROVIDER_META).map(([key, meta]) => {
                        const providerData = providers[key] || { available: false, models: [] };
                        const isExpanded = expandedProvider === key;
                        const Icon = meta.icon;
                        const modelCount = providerData.models.length;

                        return (
                            <div key={key} className={`border transition-all duration-200 ${providerData.available
                                ? 'border-zinc-700 bg-zinc-900/50'
                                : 'border-zinc-800/50 bg-zinc-950'
                                }`}>
                                {/* Card Header */}
                                <button
                                    onClick={() => {
                                        const next = isExpanded ? null : key;
                                        setExpandedProvider(next);
                                        if (next === 'bedrock') onExpandBedrock?.();
                                    }}
                                    className="w-full flex items-center justify-between p-4 text-left hover:bg-zinc-900/30 transition-colors"
                                >
                                    <div className="flex items-center gap-3">
                                        <div className={`h-2 w-2 rounded-full ${providerData.available ? 'bg-green-500 shadow-[0_0_6px_#22c55e]' : 'bg-zinc-600'}`} />
                                        <Icon className={`h-4 w-4 ${!providerData.available ? 'opacity-40 grayscale' : ''}`} style={{ color: providerData.available ? meta.color : '#71717a' }} />
                                        <div>
                                            <span className={`text-sm font-bold ${providerData.available ? 'text-white' : 'text-zinc-500'}`}>
                                                {meta.label}
                                            </span>
                                            <span className="text-[10px] text-zinc-500 ml-2">
                                                {providerData.available
                                                    ? `${modelCount} model${modelCount !== 1 ? 's' : ''}`
                                                    : key === 'ollama' ? 'Not running' : key.endsWith('_cli') ? 'Not installed' : 'No key configured'
                                                }
                                            </span>
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        {providerData.available
                                            ? <Check className="h-4 w-4 text-green-500" />
                                            : <XIcon className="h-3 w-3 text-zinc-600" />
                                        }
                                        {isExpanded ? <ChevronUp className="h-4 w-4 text-zinc-500" /> : <ChevronDown className="h-4 w-4 text-zinc-500" />}
                                    </div>
                                </button>

                                {/* Expanded Content */}
                                {isExpanded && (
                                    <div className="px-4 pb-4 space-y-3 border-t border-zinc-800/50 pt-3">
                                        <p className="text-[10px] text-zinc-500">{meta.description}</p>

                                        {/* API Key input (not for Ollama, Bedrock, or CLI — they have their own blocks) */}
                                        {key !== 'ollama' && key !== 'bedrock' && key !== 'openai_compatible' && key !== 'local_compatible' && key !== 'huggingface' && !key.endsWith('_cli') && (
                                            <div className="space-y-1.5">
                                                <label className="text-[10px] uppercase font-bold text-zinc-500">API Key</label>
                                                <div className="relative">
                                                    <input
                                                        type={visibleKeys[key] ? 'text' : 'password'}
                                                        value={getKeyValue(key)}
                                                        onChange={e => setKeyValue(key, e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors pr-8"
                                                        placeholder={meta.keyPlaceholder}
                                                    />
                                                    <button type="button" onClick={() => toggleKeyVisible(key)}
                                                        className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 transition-colors">
                                                        {visibleKeys[key] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                    </button>
                                                </div>
                                                {/* Key instructions */}
                                                {meta.keyNote && (
                                                    <p className="text-[10px] text-zinc-600">{meta.keyNote}</p>
                                                )}
                                                {meta.keyLink && (
                                                    <a
                                                        href={meta.keyLink.url}
                                                        target="_blank"
                                                        rel="noopener noreferrer"
                                                        className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors"
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
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Bedrock API Key</label>
                                                    <div className="relative">
                                                        <input type={visibleKeys['bedrock'] ? 'text' : 'password'} value={bedrockApiKey} onChange={e => setBedrockApiKey(e.target.value)}
                                                            className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors pr-8" placeholder="ABSK... or bedrock-api-key..." />
                                                        <button type="button" onClick={() => toggleKeyVisible('bedrock')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 transition-colors">
                                                            {visibleKeys['bedrock'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                    {meta.keyNote && (
                                                        <p className="text-[10px] text-zinc-600">{meta.keyNote}</p>
                                                    )}
                                                    {meta.keyLink && (
                                                        <a
                                                            href={meta.keyLink.url}
                                                            target="_blank"
                                                            rel="noopener noreferrer"
                                                            className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors"
                                                        >
                                                            <ExternalLink className="h-2.5 w-2.5" />
                                                            {meta.keyLink.label}
                                                        </a>
                                                    )}
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">AWS Region</label>
                                                    <input type="text" value={awsRegion} onChange={e => setAwsRegion(e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors" placeholder="us-east-1" />
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Inference Profile (Optional)</label>
                                                    <select
                                                        value={bedrockInferenceProfile}
                                                        onChange={(e) => setBedrockInferenceProfile(e.target.value)}
                                                        className="w-full appearance-none bg-zinc-900 border border-zinc-800 p-2.5 text-xs focus:border-white focus:outline-none transition-colors text-white cursor-pointer"
                                                    >
                                                        <option value="">None (on-demand)</option>
                                                        {loadingInferenceProfiles ? (
                                                            <option value="" disabled>Loading...</option>
                                                        ) : (
                                                            bedrockInferenceProfiles.map((p) => {
                                                                const value = (p.arn || p.id || '').toString();
                                                                const label = (p.name || p.arn || p.id || '').toString();
                                                                if (!value) return null;
                                                                return <option key={value} value={value}>{label}</option>;
                                                            })
                                                        )}
                                                    </select>
                                                </div>
                                                {inferenceProfilesError && (
                                                    <div className="flex items-start gap-2 p-2.5 bg-red-500/5 border border-red-500/20 text-[10px] text-red-400">
                                                        <Info className="w-3 h-3 mt-0.5 shrink-0" />
                                                        <span className="break-all">{inferenceProfilesError}</span>
                                                    </div>
                                                )}
                                                {!inferenceProfilesError && providerData.available && !bedrockInferenceProfile && (
                                                    <div className="flex items-start gap-2 p-2.5 bg-amber-500/5 border border-amber-500/20 text-[10px] text-amber-400">
                                                        <Info className="w-3 h-3 mt-0.5 shrink-0" />
                                                        <span>No inference profile selected. Please select an inference profile above to use AWS Bedrock.</span>
                                                    </div>
                                                )}
                                            </div>
                                        )}

                                        {/* OpenAI Compatible fields */}
                                        {key === 'openai_compatible' && (
                                            <div className="space-y-3">
                                                <div className="space-y-1.5">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">API Key</label>
                                                    <div className="relative">
                                                        <input type={visibleKeys['openai_compatible'] ? 'text' : 'password'} value={openaiCompatibleKey} onChange={e => setOpenaiCompatibleKey(e.target.value)}
                                                            className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors pr-8" placeholder="sk-..." />
                                                        <button type="button" onClick={() => toggleKeyVisible('openai_compatible')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 transition-colors">
                                                            {visibleKeys['openai_compatible'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Base URL</label>
                                                    <input type="text" value={openaiCompatibleBaseUrl} onChange={e => setOpenaiCompatibleBaseUrl(e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors" placeholder="https://openrouter.ai/api" />
                                                    <p className="text-[10px] text-zinc-600">The /v1 path is appended automatically. Do not include /v1 in the URL.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Model Names (comma-separated)</label>
                                                    <input type="text" value={openaiCompatibleModels} onChange={e => setOpenaiCompatibleModels(e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors" placeholder="e.g. meta-llama/llama-3-70b-instruct, google/gemma-2-27b-it" />
                                                    <p className="text-[10px] text-zinc-600">If the /v1/models endpoint is available, models will be fetched automatically. Otherwise, list them here.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Embedding Model Names (comma-separated)</label>
                                                    <input type="text" value={openaiCompatibleEmbedModels} onChange={e => setOpenaiCompatibleEmbedModels(e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors" placeholder="e.g. hf:nomic-ai/nomic-embed-text-v1.5" />
                                                    <p className="text-[10px] text-zinc-600">Models listed here appear in the embedding model dropdown. Models with &quot;embed&quot; in the name are also auto-detected from /v1/models.</p>
                                                </div>
                                            </div>
                                        )}

                                        {/* Local V1 Compatible fields */}
                                        {key === 'local_compatible' && (
                                            <div className="space-y-3">
                                                <div className="space-y-1.5">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">API Key (optional)</label>
                                                    <div className="relative">
                                                        <input type={visibleKeys['local_compatible'] ? 'text' : 'password'} value={localCompatibleKey} onChange={e => setLocalCompatibleKey(e.target.value)}
                                                            className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors pr-8" placeholder="Leave blank if not required" />
                                                        <button type="button" onClick={() => toggleKeyVisible('local_compatible')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 transition-colors">
                                                            {visibleKeys['local_compatible'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Base URL</label>
                                                    <input type="text" value={localCompatibleBaseUrl} onChange={e => setLocalCompatibleBaseUrl(e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors" placeholder="http://localhost:8000" />
                                                    <p className="text-[10px] text-zinc-600">The /v1 path is appended automatically. Do not include /v1 in the URL.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Model Names (comma-separated)</label>
                                                    <input type="text" value={localCompatibleModels} onChange={e => setLocalCompatibleModels(e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors" placeholder="e.g. llama-3-70b, mistral-7b" />
                                                    <p className="text-[10px] text-zinc-600">If the /v1/models endpoint is available, models will be fetched automatically. Otherwise, list them here.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Embedding Model Names (comma-separated)</label>
                                                    <input type="text" value={localCompatibleEmbedModels} onChange={e => setLocalCompatibleEmbedModels(e.target.value)}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors" placeholder="e.g. bge-m3" />
                                                    <p className="text-[10px] text-zinc-600">Models listed here appear in the embedding model dropdown. Models with &quot;embed&quot; in the name are also auto-detected from /v1/models.</p>
                                                </div>
                                            </div>
                                        )}

                                        {/* HuggingFace fields */}
                                        {key === 'huggingface' && (
                                            <div className="space-y-3">
                                                <div className="p-2.5 bg-amber-500/5 border border-amber-500/20 text-[10px] text-amber-300 leading-relaxed">
                                                    <strong>Requires torch + transformers on the host.</strong> Models load in the backend process and stay in memory. Expect 16-40 GB VRAM for 7B-class models. Without a GPU, inference runs on CPU and will be slow.
                                                </div>
                                                <div className="space-y-1.5">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Access Token (optional)</label>
                                                    <div className="relative">
                                                        <input type={visibleKeys['huggingface'] ? 'text' : 'password'} value={huggingfaceToken} onChange={e => setHuggingfaceToken(e.target.value)}
                                                            className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors pr-8" placeholder="hf_... (required for gated models like Llama, Gemma)" />
                                                        <button type="button" onClick={() => toggleKeyVisible('huggingface')}
                                                            className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300 transition-colors">
                                                            {visibleKeys['huggingface'] ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
                                                        </button>
                                                    </div>
                                                    <p className="text-[10px] text-zinc-600">Only needed for gated models. Public models load without a token.</p>
                                                </div>
                                                <div className="space-y-1">
                                                    <label className="text-[10px] uppercase font-bold text-zinc-500">Model IDs (one per line or comma-separated)</label>
                                                    <textarea value={huggingfaceModels} onChange={e => setHuggingfaceModels(e.target.value)} rows={4}
                                                        className="w-full bg-zinc-900 border border-zinc-800 p-2.5 text-xs text-white focus:border-white focus:outline-none transition-colors font-mono"
                                                        placeholder={"Qwen/Qwen2.5-7B-Instruct\nmeta-llama/Llama-3.1-8B-Instruct\nmistralai/Mistral-7B-Instruct-v0.3"}
                                                    />
                                                    <p className="text-[10px] text-zinc-600">Each ID becomes a selectable <code className="text-zinc-400">hf.&lt;org&gt;/&lt;model&gt;</code> model. First call to a model pays a 20-60s load cost, then stays warm in memory.</p>
                                                </div>
                                            </div>
                                        )}

                                        {/* Ollama info */}
                                        {key === 'ollama' && (
                                            <div className="space-y-1.5">
                                                <div className="text-[10px] text-zinc-500">
                                                    {providerData.available
                                                        ? `Detected ${modelCount} local model${modelCount !== 1 ? 's' : ''}: ${providerData.models.slice(0, 5).join(', ')}${modelCount > 5 ? '...' : ''}`
                                                        : 'Ollama is not running. Start it to use local models.'
                                                    }
                                                </div>
                                                <a
                                                    href="https://ollama.com/download"
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors"
                                                >
                                                    <ExternalLink className="h-2.5 w-2.5" />
                                                    Download Ollama →
                                                </a>
                                            </div>
                                        )}

                                        {/* CLI Sessions info */}
                                        {key.endsWith('_cli') && (
                                            <div className="space-y-2">
                                                <div className="text-[10px] text-zinc-500">
                                                    {providerData.available
                                                        ? `Detected ${modelCount} CLI session${modelCount !== 1 ? 's' : ''}: ${providerData.models.join(', ')}`
                                                        : `No CLI binary found in PATH for ${meta.label}.`
                                                    }
                                                </div>
                                                <div className="p-2.5 bg-violet-500/5 border border-violet-500/20 text-[10px] text-violet-300 leading-relaxed">
                                                    <strong>No API key needed</strong> — uses your existing CLI session. Run the CLI manually first to authenticate.
                                                </div>
                                                <div className="flex flex-col gap-1">
                                                    {key === 'anthropic_cli' && (
                                                        <a href="https://claude.ai/download" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install Claude CLI →
                                                        </a>
                                                    )}
                                                    {key === 'gemini_cli' && (
                                                        <a href="https://ai.google.dev/gemini-api/docs/gemini-cli" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install Gemini CLI →
                                                        </a>
                                                    )}
                                                    {key === 'codex_cli' && (
                                                        <a href="https://github.com/openai/codex" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install OpenAI Codex CLI →
                                                        </a>
                                                    )}
                                                    {key === 'github_copilot_cli' && (
                                                        <a href="https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli" target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[10px] text-blue-400 hover:text-blue-300 transition-colors">
                                                            <ExternalLink className="h-2.5 w-2.5" /> Install GitHub Copilot CLI →
                                                        </a>
                                                    )}
                                                </div>
                                            </div>
                                        )}

                                        {/* Available models list */}
                                        {providerData.available && providerData.models.length > 0 && key !== 'ollama' && (
                                            <div className="space-y-1">
                                                <label className="text-[10px] uppercase font-bold text-zinc-600">Available Models</label>
                                                <div className="flex flex-wrap gap-1.5">
                                                    {providerData.models.map(m => (
                                                        <span key={m} className="text-[10px] px-2 py-0.5 bg-zinc-800 text-zinc-400 border border-zinc-700/50 rounded-sm">{m}</span>
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
                <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Default Model</label>
                <p className="text-[10px] text-zinc-600 -mt-1">Used for system prompt generation and agents without a specific model assigned.</p>
                <select
                    value={selectedModel}
                    onChange={(e) => setSelectedModel(e.target.value)}
                    className="w-full appearance-none bg-zinc-900 border border-zinc-800 p-2.5 text-sm focus:border-white focus:outline-none transition-colors text-white cursor-pointer"
                >
                    {loadingModels ? (
                        <option>Loading models...</option>
                    ) : (
                        <>
                            <option value="" disabled>Select default model...</option>
                            {Object.entries(providers).map(([providerKey, info]) => {
                                if (!info.available || info.models.length === 0) return null;
                                const label = PROVIDER_META[providerKey]?.label || providerKey;
                                return (
                                    <optgroup key={providerKey} label={label}>
                                        {info.models.map((m: string) => (
                                            <option key={m} value={m}>{m}</option>
                                        ))}
                                    </optgroup>
                                );
                            })}
                        </>
                    )}
                </select>
            </div>
            {/* Default Embedding Model Selector */}
            <div className="space-y-4">
                <div className="space-y-2">
                    <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider">Embedding Model</label>
                    <p className="text-[10px] text-zinc-600 -mt-1">Used for code indexing and repository search. Requires compatible providers like Gemini, OpenAI, or Ollama.</p>
                    <select
                        value={embeddingModel}
                        onChange={(e) => setEmbeddingModel(e.target.value)}
                        className="w-full appearance-none bg-zinc-900 border border-zinc-800 p-2.5 text-sm focus:border-white focus:outline-none transition-colors text-white cursor-pointer"
                    >
                        {loadingModels ? (
                            <option>Loading models...</option>
                        ) : (
                            <>
                                <option value="">Select default embedding model...</option>
                                {Object.entries(providers).map(([providerKey, info]) => {
                                    if (!info.available || !info.embedding_models || info.embedding_models.length === 0) return null;
                                    const label = PROVIDER_META[providerKey]?.label || providerKey;
                                    return (
                                        <optgroup key={providerKey} label={label}>
                                            {info.embedding_models.map((m: string) => (
                                                <option key={m} value={m}>{m}</option>
                                            ))}
                                        </optgroup>
                                    );
                                })}
                            </>
                        )}
                    </select>
                </div>
                
                {/* Warning Message */}
                <div className="p-3 bg-amber-900/20 border border-amber-900/50 rounded-sm">
                    <p className="text-[10px] text-amber-500 leading-relaxed uppercase font-bold tracking-tight">
                        ⚠ Warning: Changing the global embedding model will affect new repository indexals. 
                        Existing repositories will NOT be automatically migrated. Use individual repo settings to re-index manually if needed.
                    </p>
                </div>
            </div>

            <div className="pt-4 flex justify-end">
                <button
                    onClick={onSave}
                    disabled={isSaving}
                    className="flex items-center gap-2 px-6 py-2.5 text-sm font-bold bg-white text-black hover:bg-zinc-200 transition-all shadow-lg disabled:opacity-50 disabled:cursor-not-allowed"
                >
                    {isSaving && <Loader2 className="w-4 h-4 animate-spin" />}
                    {isSaving ? 'Saving…' : 'Save Changes'}
                </button>
            </div>
        </div>
    );
};
