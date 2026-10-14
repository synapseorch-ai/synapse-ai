import { ExternalLink, RefreshCw } from 'lucide-react';
import { Button, Combobox, Field, Input, Section, Textarea } from '@/components/ui';
import { ProviderCard } from '@/components/providers/ProviderCard';
import { FORM } from './form';
import { modelOptions as buildModelOptions, providerLabel, withUnreachable } from '@/lib/models';

/** `builder_model` is empty when it should follow `model`; a Combobox needs a value. */
const DEFAULT_SENTINEL = '__default__';
import React, { useState } from 'react';
import { KnowledgeModels, type KnowledgeModelSettings } from './KnowledgeModels';
import { useSavedSecrets } from './SavedSecret';

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
    /** Why its list could not be read: a hosted provider that refused the key (PROV1). */
    error?: string;
}

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
    /** A caution above the key, in the warning tone. */
    warning?: string;
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
        warning: 'Requires torch + transformers on the host. Models load in the backend process and stay in memory. Expect 16-40 GB VRAM for 7B-class models. Without a GPU, inference runs on CPU and will be slow.',
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
    // The hosted OpenAI-style providers (PROV1, D245): a key each, nothing else.
    // Their marks (2026-10-10): Groq's and Z.ai's are their own sites' favicon SVGs; Kimi's and OpenRouter's are
    // their favicons' tiles redrawn from LobeHub's icons (MIT); the rest are LobeHub's colour marks, each checked
    // against the provider's own site. Every one reads on both grounds, so none needs `theme-adaptive-icon`.
    mistral: {
        img: '/mistral-icon.svg',
        description: 'Mistral Large, Medium and Small, with reasoning on Small and Medium.',
        keyLink: { label: 'Get your key at Mistral La Plateforme →', url: 'https://console.mistral.ai/api-keys' },
    },
    moonshot: {
        img: '/kimi-icon.svg',
        description: 'Kimi models from Moonshot AI: long context, and strong with tools. No free tier.',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at the Kimi platform →', url: 'https://platform.kimi.ai/console/api-keys' },
    },
    qwen: {
        img: '/qwen-icon.svg',
        description: 'Qwen models from Alibaba Cloud Model Studio (international).',
        keyPlaceholder: 'sk-...',
        keyLink: { label: 'Get your key at Alibaba Cloud Model Studio →', url: 'https://modelstudio.console.alibabacloud.com/model/settings/api-key' },
    },
    zai: {
        img: '/zai-icon.svg',
        description: 'GLM models from Z.ai (Zhipu). GLM-4.7 Flash is free.',
        keyLink: { label: 'Get your key at Z.ai →', url: 'https://z.ai/manage-apikey/apikey-list' },
    },
    minimax: {
        img: '/minimax-icon.svg',
        description: 'MiniMax M3 and M2 models, built for agents.',
        keyLink: { label: 'Get your key at the MiniMax platform →', url: 'https://platform.minimax.io/user-center/basic-information/interface-key' },
    },
    openrouter: {
        img: '/openrouter-icon.svg',
        description: 'One key for hundreds of models from many labs, billed by OpenRouter.',
        keyPlaceholder: 'sk-or-...',
        keyLink: { label: 'Get your key at OpenRouter →', url: 'https://openrouter.ai/settings/keys' },
    },
    groq: {
        img: '/groq-icon.svg',
        description: 'Open models on Groq: very fast answers, with a free tier.',
        keyPlaceholder: 'gsk_...',
        keyLink: { label: 'Get your key at GroqCloud →', url: 'https://console.groq.com/keys' },
    },
    cerebras: {
        img: '/cerebras-icon.svg',
        description: 'Open models on Cerebras: very fast answers, with a free tier.',
        keyPlaceholder: 'csk-...',
        keyLink: { label: 'Get your key at Cerebras Cloud →', url: 'https://cloud.cerebras.ai/' },
    },
    together: {
        img: '/together-icon.svg',
        description: 'Open models (Kimi, GLM, DeepSeek, Qwen, gpt-oss and more) hosted by Together AI.',
        keyLink: { label: 'Get your key at Together AI →', url: 'https://api.together.ai/settings/api-keys' },
    },
    fireworks: {
        img: '/fireworks-icon.svg',
        description: 'Open models hosted by Fireworks AI.',
        keyLink: { label: 'Get your key at Fireworks AI →', url: 'https://app.fireworks.ai/settings/users/api-keys' },
    },
    nvidia: {
        img: '/nvidia-icon.svg',
        description: 'Open models hosted by NVIDIA (NIM): a free trial for evaluating them, not for production.',
        keyPlaceholder: 'nvapi-...',
        keyLink: { label: 'Get your key at NVIDIA build →', url: 'https://build.nvidia.com/settings/api-keys' },
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

/**
 * The providers in three plain groups (Naveen, PROV1): who makes the models, who
 * hosts others' models, and what runs on your own machine or server. Every card
 * is in one; a provider missing from these lists still shows, at the end.
 */
const PROVIDER_GROUPS: { title: string; ids: string[] }[] = [
    { title: 'Model makers', ids: ['openai', 'anthropic', 'gemini', 'grok', 'deepseek', 'mistral', 'moonshot', 'qwen', 'zai', 'minimax'] },
    { title: 'Hosts', ids: ['bedrock', 'openrouter', 'groq', 'cerebras', 'together', 'fireworks', 'nvidia'] },
    { title: 'On your machine or server', ids: ['ollama', 'openai_compatible', 'local_compatible', 'huggingface', 'anthropic_cli', 'gemini_cli', 'codex_cli', 'github_copilot_cli'] },
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
    knowledgeModels, setKnowledgeModels,
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
    const embeddingOptions = withUnreachable(
        buildModelOptions(providers, 'embedding_models'), embeddingModel,
    );
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

    /** The setting that holds each provider's key, where its card saves one. */
    const secretOf = (key: string): string | undefined => {
        if (key === 'bedrock') return 'bedrock_api_key';
        if (key === 'huggingface') return 'huggingface_token';
        if (key === 'ollama' || key.endsWith('_cli')) return undefined;
        return `${key}_key`;
    };
    const KEY_LABELS: Record<string, string> = {
        bedrock: 'Bedrock API key', huggingface: 'Access token (optional)', local_compatible: 'API key (optional)',
    };
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
            return (
                <div className="space-y-2">
                    <div className="text-2xs text-text-faint">
                        {providerData.available
                            ? `Detected ${modelCount} CLI session${modelCount !== 1 ? 's' : ''}: ${providerData.models.join(', ')}`
                            : `No CLI binary found in PATH for ${providerLabel(key)}.`}
                    </div>
                    <div className="rounded-md bg-surface-2 px-3 py-2.5 text-xs leading-relaxed text-text-muted">
                        <strong>No API key needed</strong> — uses your existing CLI session. Run the CLI manually first to authenticate.
                    </div>
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
            {/* Cloud's order (Naveen, PROV1): what the workspace uses first, then Knowledge, then the
                providers that make the models callable, then the rerankers. */}
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
                <Field label="Builder model" hint="Used by the AI builder for every step of a build, except the quick steps when you pick a model for them below. Reasoning-capable models do noticeably better here.">
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
                    approved — on their own model, often a small one. Empty follows the builder model. */}
                <Field label="Builder model for quick steps" hint="Used by the AI builder to decide what you asked for and whether you approved a plan. A small, fast model usually does this as well as a large one.">
                    <Combobox
                        value={builderQuickModel || DEFAULT_SENTINEL}
                        onChange={(v) => setBuilderQuickModel(v === DEFAULT_SENTINEL ? '' : v)}
                        options={quickOptions}
                        placeholder={loadingModels ? 'Loading models…' : 'Select a model for quick steps…'}
                        searchPlaceholder="Search models…"
                        aria-label="Builder model for quick steps"
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
                        value={embeddingModel || undefined}
                        onChange={setEmbeddingModel}
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
                embeddingModel={embeddingModel} chatModel={selectedModel} />

            {/* The form's one save, at its foot under the last field, on the right of its column: no rule
                of its own, so it reads as part of Defaults and Knowledge, not a section before Providers. */}
            <div className={`${FORM} flex justify-end pb-5`}>
                <Button onClick={onSave} loading={isSaving}>{isSaving ? 'Saving…' : 'Save changes'}</Button>
            </div>

            {/* Provider cards, cloud's card synced (PROV1): each saves and removes its own key. */}
            <Section variant="plain" collapsible={false} title="Providers"
                description="Keys are kept in this install and never shown again — only a fragment, so you can tell which is in place. Bring as many as you have."
                actions={
                    <Button variant="secondary" size="sm" onClick={onRefresh} loading={loadingModels}>
                        <RefreshCw className="size-3.5" aria-hidden /> Refresh
                    </Button>
                }>
                <div className={`${FORM} space-y-6`}>
                    {PROVIDER_SECTIONS.map(group => (
                    <div key={group.title} className="space-y-2" data-provider-group={group.title}>
                    <h3 className="text-xs font-medium text-text-muted">{group.title}</h3>
                    {group.ids.filter(key => key in PROVIDER_META).map(key => {
                        const meta = PROVIDER_META[key];
                        const providerData = providers[key] || { available: false, models: [] };
                        const modelCount = providerData.models.length;
                        const secret = secretOf(key);
                        const stored = secret ? secrets[secret] : undefined;
                        return (
                            <ProviderCard
                                key={key}
                                meta={{
                                    label: providerLabel(key),
                                    icon: meta.img,
                                    iconClassName: meta.img === OLLAMA_IMG ? 'theme-adaptive-icon' : undefined,
                                    description: meta.description,
                                    keyPlaceholder: meta.keyPlaceholder,
                                    keyLink: meta.keyLink,
                                    keyNote: meta.keyNote,
                                    warning: meta.warning,
                                }}
                                live={providerData.available}
                                error={providerData.error}
                                status={providerData.available
                                    ? `${modelCount} model${modelCount !== 1 ? 's' : ''}`
                                    : key === 'ollama' ? 'Not running' : key.endsWith('_cli') ? 'Not installed' : 'No key configured'}
                                keyState={stored ? { set: stored.set, hint: stored.hint ? `…${stored.hint}` : null } : undefined}
                                keyLabel={KEY_LABELS[key]}
                                onSaveKey={secret ? (v: string) => saveSecret(secret, v) : undefined}
                                onRemoveKey={secret ? async () => { await removeSecret(secret); } : undefined}
                                extras={extrasFor(key, providerData)}
                                models={key === 'ollama' || key.endsWith('_cli') ? [] : providerData.models}
                                open={expandedProvider === key}
                                onToggle={() => setExpandedProvider(expandedProvider === key ? null : key)}
                            />
                        );
                    })}
                    </div>
                    ))}
                </div>
            </Section>

            <KnowledgeModels part="rerankers" value={knowledgeModels} onChange={setKnowledgeModels} providers={providers}
                embeddingModel={embeddingModel} chatModel={selectedModel} onSave={onSave} isSaving={isSaving} />
        </div>
    );
};
