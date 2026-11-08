/**
 * How each provider is shown on its card: its mark, its words, where its key goes
 * (ONB2, D303). Moved out of Settings → Models unchanged, so the first-run model
 * step (`components/onboarding`) builds the same cards as that screen.
 */
import { providerLabel } from '@/lib/models';
import type { ProviderCardMeta } from '@/components/providers/ProviderCard';

// Every brand mark is decoration: it sits beside the provider's own name, so an
// `alt` would make a screen reader read that name twice — and the Local V1
// Compatible card borrows Ollama's mark, so it read out the wrong name entirely.
// Keep these silent; the visible label is what names the card (F44). A provider
// with no logo of ours is a theme-aware letter tile (`AppMark`, M2f), not a
// brand hex that stays one colour on both grounds (M2g).
export const OLLAMA_IMG = '/ollama-icon.svg';
export const COPILOT_IMG = '/github-copilot-icon.svg';

export interface ProviderInfo {
    available: boolean;
    models: string[];
    embedding_models?: string[];
    /** Why its list could not be read: a hosted provider that refused the key (PROV1). */
    error?: string;
    /** A model CLI's own word on itself, asked without a model call (CLIM1). */
    cli?: CliInfo;
    /** Models its provider said are gone, left out of `models`, each with the provider's words (F1213). */
    unavailable?: { model: string; reason: string }[];
}

/** What `/api/models` says of an installed model CLI (CLIM1, D299). */
export interface CliInfo {
    version: string | null;
    /** null: it cannot be told without a model call. */
    signed_in: boolean | null;
    sign_in_hint: string | null;
    /** "cli": the CLI listed its models itself; "list": Synapse's list for its version. */
    models_source: 'cli' | 'list' | null;
    models_note: string | null;
    /** Something it reads that Synapse cannot switch off. */
    note: string | null;
}

export interface ProviderMeta {
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

export const PROVIDER_META: Record<string, ProviderMeta> = {
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
    // Codex has no mark of its own in Simple Icons; it is OpenAI's, as the OpenAI card's (CLIM1).
    codex_cli: {
        img: '/chatgpt-icon.svg',
        description: 'Use the locally installed OpenAI Codex CLI agent. No API key needed — uses your existing terminal session.',
    },
    // GitHub Copilot's mark (Simple Icons 16.35.0), black: inverted on the dark ground like Ollama's.
    github_copilot_cli: {
        img: COPILOT_IMG,
        description: 'Use the locally installed GitHub Copilot CLI. No API key needed — uses your existing GitHub session.',
    },
};

/** The setting that holds each provider's key, where its card saves one. */
export const secretOf = (key: string): string | undefined => {
    if (key === 'bedrock') return 'bedrock_api_key';
    if (key === 'huggingface') return 'huggingface_token';
    if (key === 'ollama' || key.endsWith('_cli')) return undefined;
    return `${key}_key`;
};
export const KEY_LABELS: Record<string, string> = {
    bedrock: 'Bedrock API key', huggingface: 'Access token (optional)', local_compatible: 'API key (optional)',
};

/** A card's head: its name, mark and key words. */
export const cardMeta = (key: string): ProviderCardMeta => {
    const meta = PROVIDER_META[key];
    return {
        label: providerLabel(key),
        icon: meta.img,
        iconClassName: meta.img === OLLAMA_IMG || meta.img === COPILOT_IMG ? 'theme-adaptive-icon' : undefined,
        description: meta.description,
        keyPlaceholder: meta.keyPlaceholder,
        keyLink: meta.keyLink,
        keyNote: meta.keyNote,
        warning: meta.warning,
    };
};

/** Whether a card is lit, and the words beside its dot. */
export const cardStatus = (key: string, providerData: ProviderInfo): { live: boolean; status: string } => {
    const modelCount = providerData.models.length;
    return {
        live: providerData.available && providerData.cli?.signed_in !== false,
        status: providerData.available
            ? (providerData.cli?.signed_in === false ? 'Not signed in' : `${modelCount} model${modelCount !== 1 ? 's' : ''}`)
            : key === 'ollama' ? 'Not running' : key.endsWith('_cli') ? 'Not installed' : 'No key configured',
    };
};
