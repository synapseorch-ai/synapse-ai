"use client";
/* eslint-disable @typescript-eslint/ban-ts-comment */
/* eslint-disable react/no-unescaped-entities */
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect } from 'react';

import { useRouter } from 'next/navigation';
import { X } from 'lucide-react';
import { useDispatch, useSelector } from 'react-redux';
import { RootState, AppDispatch } from '@/store';
import { fetchAllSettingsData } from '@/store/settingsSlice';

import type { Tab } from './settings/types';
import { LinkButton, Screen } from '@/components/ui';
import { SettingsNav } from '@/components/app/SettingsNav';
import { isSettingsEntry, navEntryFor } from '@/lib/nav';
import { AUTH_CHANGED_EVENT, readAuthStatus } from '@/lib/auth';
import { KNOWLEDGE_SWITCHED_EVENT, type RerankListing } from '@/lib/knowledge';
import { BOARDS_SWITCHED_EVENT } from '@/components/boards/board';
import { HOSTED_KEY_NAMES } from '@/lib/models';
import { GeneralTab, type BoardsSettings } from './settings/GeneralTab';
import type { ToolPolicy } from './settings/ToolPolicy';
import { PersonalDetailsTab } from './settings/PersonalDetailsTab';
import { VariablesTab } from './settings/VariablesTab';
import { MemoryTab } from './settings/MemoryTab';
import { DataLabTab } from './settings/DataLabTab';
import { ModelsTab } from './settings/ModelsTab';
import {
    KNOWLEDGE_MODEL_DEFAULTS, KNOWLEDGE_SECRETS, type KnowledgeModelSettings,
} from './settings/KnowledgeModels';
import { SavedSecretsContext, pickSecrets, type SecretState } from './settings/SavedSecret';
import { clearSecret, postSecret } from '@/lib/providerSecrets';
import { IntegrationsTab } from './settings/IntegrationsTab';
import { ToastNotification } from './settings/ToastNotification';
import { ReposTab } from './settings/ReposTab';
import { DBsTab } from './settings/DBsTab';
import { ImportExportTab } from './settings/ImportExportTab';
import { SupportTab } from './settings/SupportTab';
import { APIKeysTab } from './settings/APIKeysTab';
import { McpServerTab } from './settings/McpServerTab';
import { ScaleTab } from './settings/ScaleTab';


export const SettingsView = ({ initialTab = 'general', initialSubTab }: { initialTab?: string; initialSubTab?: string }) => {
    const dispatch = useDispatch<AppDispatch>();
    const { models: rModels, initialized } = useSelector((state: RootState) => state.settings);

    const activeTab = initialTab as Tab;
    const [agentName, setAgentName] = useState('');
    const [selectedModel, setSelectedModel] = useState('');
    const [embeddingModel, setEmbeddingModel] = useState('');
    const [builderModel, setBuilderModel] = useState('');
    const [builderQuickModel, setBuilderQuickModel] = useState('');
    // The workspace response cap (F263), as typed: '' is automatic.
    const [maxOutputTokens, setMaxOutputTokens] = useState('');
    // The workspace reasoning effort (F269): the setting existed, nothing set it.
    const [reasoningEffort, setReasoningEffort] = useState('medium');
    const [mode, setMode] = useState('local'); // local | cloud
    const [localModels, setLocalModels] = useState<string[]>([]);
    const [cloudModels, setCloudModels] = useState<string[]>([]);
    const [providers, setProviders] = useState<Record<string, { available: boolean; models: string[]; embedding_models?: string[] }>>({});
    const [loadingModels, setLoadingModels] = useState(false);
    const [isSaving, setIsSaving] = useState(false);

    const router = useRouter();

    // Vault settings
    const [vaultEnabled, setVaultEnabled] = useState(true);
    const [vaultThreshold, setVaultThreshold] = useState(100000);
    const [autoCompactEnabled, setAutoCompactEnabled] = useState(false);
    const [autoCompactThreshold, setAutoCompactThreshold] = useState(50000);
    // A step compacts at this share of its own model's window (D339 P1); 0 keeps the fixed character limit.
    const [autoCompactWindowPct, setAutoCompactWindowPct] = useState(50);
    const [allowDbWrite, setAllowDbWrite] = useState(false);
    // Knowledge's switch (F367); null until read, and not saved until then, so a failed read never turns it off.
    const [knowledgeEnabled, setKnowledgeEnabled] = useState<boolean | null>(null);
    // Boards' switch and caps (BOARD1b): as Knowledge's, null until read and not saved until then.
    const [boards, setBoards] = useState<BoardsSettings | null>(null);
    // The install's ceiling on a Team agent's members at once (TEAM1b): null until read, not saved until then.
    const [teamCeiling, setTeamCeiling] = useState<number | null>(null);
    // What an agent does at its turn limit (F1219): '' is the default (ask in chats, stop in unattended runs); null
    // until read, not saved until then.
    const [turnLimitMode, setTurnLimitMode] = useState<string | null>(null);
    const [toolPolicyDefaults, setToolPolicyDefaults] = useState<ToolPolicy>({});
    const [bashAllowedDirs, setBashAllowedDirs] = useState<string[]>([]);

    // Keys
    const [openaiKey, setOpenaiKey] = useState('');
    const [anthropicKey, setAnthropicKey] = useState('');
    const [geminiKey, setGeminiKey] = useState('');
    const [grokKey, setGrokKey] = useState('');
    const [deepseekKey, setDeepseekKey] = useState('');
    // The hosted providers' keys (PROV1), by setting name; one map, not eleven states.
    const [hostedKeys, setHostedKeys] = useState<Record<string, string>>({});
    const setHostedKey = (name: string, v: string) => setHostedKeys(k => ({ ...k, [name]: v }));
    const [bedrockApiKey, setBedrockApiKey] = useState('');
    const [openaiCompatibleKey, setOpenaiCompatibleKey] = useState('');
    const [openaiCompatibleBaseUrl, setOpenaiCompatibleBaseUrl] = useState('');
    const [openaiCompatibleModels, setOpenaiCompatibleModels] = useState('');
    const [openaiCompatibleEmbedModels, setOpenaiCompatibleEmbedModels] = useState('');
    const [localCompatibleBaseUrl, setLocalCompatibleBaseUrl] = useState('');
    const [localCompatibleKey, setLocalCompatibleKey] = useState('');
    const [localCompatibleModels, setLocalCompatibleModels] = useState('');
    const [localCompatibleEmbedModels, setLocalCompatibleEmbedModels] = useState('');
    const [huggingfaceToken, setHuggingfaceToken] = useState('');
    const [huggingfaceModels, setHuggingfaceModels] = useState('');
    const [anthropicCliModels, setAnthropicCliModels] = useState('');
    const [geminiCliModels, setGeminiCliModels] = useState('');
    const [codexCliModels, setCodexCliModels] = useState('');
    const [githubCopilotCliModels, setGithubCopilotCliModels] = useState('');
    const [knowledgeModels, setKnowledgeModels] = useState<KnowledgeModelSettings>(KNOWLEDGE_MODEL_DEFAULTS);
    const [transformRuntime, setTransformRuntime] = useState<'docker' | 'host'>('docker');
    const [awsRegion, setAwsRegion] = useState('us-east-1');
    const [sqlConnectionString, setSqlConnectionString] = useState('');


    // Personal Details
    const [pdFirstName, setPdFirstName] = useState('');
    const [pdLastName, setPdLastName] = useState('');
    const [pdEmail, setPdEmail] = useState('');
    const [pdPhone, setPdPhone] = useState('');
    const [pdAddress1, setPdAddress1] = useState('');
    const [pdAddress2, setPdAddress2] = useState('');
    const [pdCity, setPdCity] = useState('');
    const [pdState, setPdState] = useState('');
    const [pdZipcode, setPdZipcode] = useState('');

    // Integrations: n8n
    const [n8nUrl, setN8nUrl] = useState('http://localhost:5678');
    const [n8nApiKey, setN8nApiKey] = useState('');

    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'warning' | 'error' } | null>(null);
    const showToast = (message: string, type: 'success' | 'warning' | 'error' = 'success') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), 4000);
    };

    const [embedCode, setEmbedCode] = useState(false);

    // Login settings
    const [loginEnabled, setLoginEnabled] = useState(false);
    const [loginUsername, setLoginUsername] = useState('');
    const [isLoginSaving, setIsLoginSaving] = useState(false);

    const refreshBedrockModels = async () => {
        setLoadingModels(true);
        try {
            const res = await fetch('/api/bedrock/models');
            const data = await res.json();
            const bedrock = Array.isArray(data.models) ? data.models : [];
            if (bedrock.length > 0) {
                setCloudModels(prev => {
                    const nonBedrock = (prev || []).filter((m: string) => !m.startsWith('bedrock.'));
                    return [...nonBedrock, ...bedrock];
                });
            }
        } catch {
            // ignore
        } finally {
            setLoadingModels(false);
        }
    };

    // The rerank models the saved keys can use (BF9), read beside the chat models: a key saved or removed changes
    // both. Knowledge's, so a 404 while it is off is no list. `force` asks every provider again (the Refresh button).
    const [rerankListings, setRerankListings] = useState<RerankListing[]>([]);
    const refreshRerank = async (force: boolean) => {
        try {
            const res = await fetch(`/api/models/rerank${force ? '?refresh=1' : ''}`);
            const data = res.ok ? await res.json() : null;
            setRerankListings(Array.isArray(data?.providers) ? data.providers : []);
        } catch {
            // ignore: the pickers keep what they had
        }
    };

    const refreshModels = async (force = false) => {
        setLoadingModels(true);
        void refreshRerank(force);
        try {
            // Refresh asks the model CLIs again too: their word is kept ten minutes (CLIM1).
            const res = await fetch(`/api/models${force ? '?refresh=1' : ''}`);
            const data = await res.json();
            setLocalModels(data.local || []);
            setCloudModels(data.cloud || []);
            if (data.providers) setProviders(data.providers);
        } catch {
            // ignore
        } finally {
            setLoadingModels(false);
        }
    };

    // Keys are never served back (F201): each arrives blank beside whether one
    // is saved. A blank field on save keeps the saved key; Remove clears it.
    const [secretState, setSecretState] = useState<Record<string, SecretState>>({});
    const secretSetters: Record<string, (v: string) => void> = {
        openai_key: setOpenaiKey, anthropic_key: setAnthropicKey, gemini_key: setGeminiKey,
        grok_key: setGrokKey, deepseek_key: setDeepseekKey, bedrock_api_key: setBedrockApiKey,
        openai_compatible_key: setOpenaiCompatibleKey, local_compatible_key: setLocalCompatibleKey,
        huggingface_token: setHuggingfaceToken, n8n_api_key: setN8nApiKey,
        ...Object.fromEntries(HOSTED_KEY_NAMES.map(name => [name, (v: string) => setHostedKey(name, v)])),
        ...Object.fromEntries(KNOWLEDGE_SECRETS.map(name => [
            name, (v: string) => setKnowledgeModels(k => ({ ...k, [name]: v })),
        ])),
    };
    const SECRET_NAMES = Object.keys(secretSetters);

    const removeSecret = async (name: string) => {
        try {
            setSecretState(pickSecrets(await clearSecret(name), SECRET_NAMES));
            await refreshModels();
            showToast('Key removed', 'success');
        } catch {
            showToast('Could not remove the key', 'error');
        }
    };

    // A provider card saves its own key (PROV1, cloud's card): a POST of that
    // one setting, which the route merges into what is stored.
    const saveSecret = async (name: string, value: string) => {
        setSecretState(pickSecrets(await postSecret(name, value), SECRET_NAMES));
        await refreshModels();
        showToast('Key saved', 'success');
    };

    const handleSaveSection = async () => {
        setIsSaving(true);
        const payload = {
            agent_name: agentName,
            model: selectedModel,
            embedding_model: embeddingModel,
            builder_model: builderModel,
            builder_quick_model: builderQuickModel,
            // Blank is automatic — null clears a stored cap (F263).
            max_output_tokens: maxOutputTokens.trim() ? Number(maxOutputTokens) : null,
            reasoning_effort: reasoningEffort,
            mode: mode,
            openai_key: openaiKey,
            anthropic_key: anthropicKey,
            gemini_key: geminiKey,
            grok_key: grokKey,
            deepseek_key: deepseekKey,
            ...Object.fromEntries(HOSTED_KEY_NAMES.map(name => [name, hostedKeys[name] ?? ''])),
            openai_compatible_key: openaiCompatibleKey,
            openai_compatible_base_url: openaiCompatibleBaseUrl,
            openai_compatible_models: openaiCompatibleModels,
            openai_compatible_embed_models: openaiCompatibleEmbedModels,
            local_compatible_base_url: localCompatibleBaseUrl,
            local_compatible_key: localCompatibleKey,
            local_compatible_models: localCompatibleModels,
            local_compatible_embed_models: localCompatibleEmbedModels,
            huggingface_token: huggingfaceToken,
            huggingface_models: huggingfaceModels,
            anthropic_cli_models: anthropicCliModels,
            gemini_cli_models: geminiCliModels,
            codex_cli_models: codexCliModels,
            github_copilot_cli_models: githubCopilotCliModels,
            bedrock_api_key: bedrockApiKey,
            aws_region: awsRegion,
            sql_connection_string: sqlConnectionString,
            n8n_url: n8nUrl,
            n8n_api_key: n8nApiKey,
            vault_enabled: vaultEnabled,
            vault_threshold: vaultThreshold,
            auto_compact_enabled: autoCompactEnabled,
            auto_compact_threshold: autoCompactThreshold,
            auto_compact_window_pct: autoCompactWindowPct,
            allow_db_write: allowDbWrite,
            tool_policy_defaults: toolPolicyDefaults,
            embed_code: embedCode,
            bash_allowed_dirs: bashAllowedDirs,
            ...(knowledgeEnabled === null ? {} : { knowledge_enabled: knowledgeEnabled }),
            ...(boards === null ? {} : {
                boards_enabled: boards.enabled, boards_max_open_cards: boards.maxOpenCards, boards_max_shared: boards.maxShared,
            }),
            ...(teamCeiling === null ? {} : { team_at_once_ceiling: teamCeiling }),
            ...(turnLimitMode === null ? {} : { turn_limit_mode: turnLimitMode }),
            ...knowledgeModels,
        };

        try {
            const response = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!response.ok) throw new Error('Failed to update settings');
            if (knowledgeEnabled !== null) window.dispatchEvent(new Event(KNOWLEDGE_SWITCHED_EVENT));
            if (boards !== null) window.dispatchEvent(new Event(BOARDS_SWITCHED_EVENT));
            // The answer serves no key either: note what is now saved, and empty
            // the fields a new key was typed into, which read "Saved" from here.
            const saved = await response.json().catch(() => null);
            if (saved) {
                setSecretState(pickSecrets(saved, SECRET_NAMES));
                SECRET_NAMES.forEach(n => secretSetters[n](''));
            }

            if (mode === 'bedrock' || bedrockApiKey || secretState.bedrock_api_key?.set) {
                await refreshBedrockModels();
            } else if (activeTab === 'models' || mode === 'cloud') {
                await refreshModels();
            }
            showToast('Configuration saved', 'success');
        } catch (error) {
            console.error(error);
            showToast('Failed to save settings', 'error');
        } finally {
            setIsSaving(false);
        }
    };

    /**
     * Turning login on must not sign out the person doing it (F221). With a
     * password just typed, sign in with it; otherwise (on again with the saved
     * one) a session this browser already holds is enough.
     */
    const staySignedIn = async (username: string, password: string): Promise<boolean> => {
        if (password) {
            const res = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password }),
            }).catch(() => null);
            if (res?.ok) return true;
        }
        return (await readAuthStatus())?.authenticated === true;
    };

    /** True once saved; `loginEnabled` changes only then, so the switch never
     * shows a state the backend does not hold (F222). */
    const handleSaveLogin = async (enabled: boolean, username: string, password: string): Promise<boolean> => {
        setIsLoginSaving(true);
        try {
            const res = await fetch('/api/settings/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ login_enabled: enabled, login_username: username, login_password: password }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail || 'Failed to save login settings');
            }
            setLoginEnabled(enabled);
            setLoginUsername(username);
            if (enabled && !(await staySignedIn(username, password))) {
                // No session and no password to make one: sign in once, then
                // come back here — rather than a page whose every call is now
                // refused, with no word why.
                window.location.assign(`/login?redirect=${encodeURIComponent(window.location.pathname)}`);
                return true;
            }
            showToast(enabled ? `Login is on — you're signed in as ${username}` : 'Login disabled', 'success');
            window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
            return true;
        } catch (e: any) {
            showToast(e.message || 'Failed to save login settings', 'error');
            return false;
        } finally {
            setIsLoginSaving(false);
        }
    };

    const handleSavePersonalDetails = async () => {
        try {
            const res = await fetch('/api/personal-details', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    first_name: pdFirstName,
                    last_name: pdLastName,
                    email: pdEmail,
                    phone_number: pdPhone,
                    address: {
                        address1: pdAddress1,
                        address2: pdAddress2,
                        city: pdCity,
                        state: pdState,
                        zipcode: pdZipcode
                    }
                })
            });
            if (!res.ok) throw new Error('Failed to save personal details');
            showToast('Personal details saved', 'success');
        } catch {
            showToast('Error saving personal details', 'error');
        }
    };

    // Data Lab State
    const [dlTopic, setDlTopic] = useState('');
    const [dlCount, setDlCount] = useState(10);
    const [dlModel, setDlModel] = useState('');
    const [dlSystemPrompt, setDlSystemPrompt] = useState('You are a helpful assistant.');
    const [dlEdgeCases, setDlEdgeCases] = useState('');
    const [dlStatus, setDlStatus] = useState<any>(null);
    const [dlDatasets, setDlDatasets] = useState<any[]>([]);

    useEffect(() => {
        if (activeTab === 'datalab') {
            // Initial fetch
            fetchDatasets();
            fetchStatus();
            // Poll
            const interval = setInterval(fetchStatus, 2000);
            return () => clearInterval(interval);
        }
    }, [activeTab]);

    // A job that just ended shows its dataset. The poll above used to test
    // `dlStatus` inside its own closure — the value from when the tab opened —
    // so the list refreshed only on a reload.
    const dlJobStatus = dlStatus?.status;
    useEffect(() => {
        if (activeTab === 'datalab' && (dlJobStatus === 'completed' || dlJobStatus === 'failed')) fetchDatasets();
    }, [activeTab, dlJobStatus]);

    const fetchDatasets = () => fetch('/api/synthetic/datasets').then(r => r.json()).then(setDlDatasets).catch(() => { });
    const fetchStatus = () => fetch('/api/synthetic/status').then(r => r.json()).then(setDlStatus).catch(() => { });

    const handleGenerateData = async () => {
        if (!dlTopic) { showToast('Please enter a topic', 'warning'); return; }

        try {
            const res = await fetch('/api/synthetic/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    topic: dlTopic,
                    count: dlCount,
                    model: dlModel || null,
                    system_prompt: dlSystemPrompt,
                    edge_cases: dlEdgeCases
                })
            });
            if (res.ok) {
                showToast('Generation started!', 'success');
                fetchStatus();
            } else {
                const err = await res.json();
                showToast('Error: ' + err.detail, 'error');
            }
        } catch (e) {
            showToast('Failed to start generation', 'error');
        }
    };

    // Fetch data on open
    useEffect(() => {
        // Sync models local state with Redux to avoid breaking existing dropdown refs mapping
        if (initialized) {
            setLocalModels(rModels.local || []);
            setCloudModels(rModels.cloud || []);
            setProviders(rModels.providers || {});
        } else {
            dispatch(fetchAllSettingsData());
        }

        // Get settings
        fetch('/api/settings')
            .then(res => res.json())
            .then(data => {
                setAgentName(data.agent_name || 'Antigravity Agent');
                setSelectedModel(data.model || 'ollama.mistral');
                setEmbeddingModel(data.embedding_model || '');
                setBuilderModel(data.builder_model || '');
                setBuilderQuickModel(data.builder_quick_model || '');
                setMaxOutputTokens(data.max_output_tokens ? String(data.max_output_tokens) : '');
                setReasoningEffort(data.reasoning_effort || 'medium');
                setMode(data.mode || 'local');
                setSecretState(pickSecrets(data, SECRET_NAMES));
                setOpenaiKey(data.openai_key || '');
                setAnthropicKey(data.anthropic_key || '');
                setGeminiKey(data.gemini_key || '');
                setGrokKey(data.grok_key || '');
                setDeepseekKey(data.deepseek_key || '');
                setHostedKeys(Object.fromEntries(HOSTED_KEY_NAMES.map(name => [name, data[name] || ''])));
                setOpenaiCompatibleKey(data.openai_compatible_key || '');
                setOpenaiCompatibleBaseUrl(data.openai_compatible_base_url || '');
                setOpenaiCompatibleModels(data.openai_compatible_models || '');
                setOpenaiCompatibleEmbedModels(data.openai_compatible_embed_models || '');
                setLocalCompatibleBaseUrl(data.local_compatible_base_url || '');
                setLocalCompatibleKey(data.local_compatible_key || '');
                setLocalCompatibleModels(data.local_compatible_models || '');
                setLocalCompatibleEmbedModels(data.local_compatible_embed_models || '');
                setHuggingfaceToken(data.huggingface_token || '');
                setHuggingfaceModels(data.huggingface_models || '');
                setAnthropicCliModels(data.anthropic_cli_models || '');
                setGeminiCliModels(data.gemini_cli_models || '');
                setCodexCliModels(data.codex_cli_models || '');
                setGithubCopilotCliModels(data.github_copilot_cli_models || '');
                setKnowledgeModels({
                    ...KNOWLEDGE_MODEL_DEFAULTS,
                    knowledge_embedding_model: data.knowledge_embedding_model || '',
                    knowledge_contextual_model: data.knowledge_contextual_model || '',
                    knowledge_rerank_model: data.knowledge_rerank_model || '',
                    knowledge_rerank_base_url: data.knowledge_rerank_base_url || '',
                });
                setTransformRuntime((data.transform_runtime === 'host' ? 'host' : 'docker'));
                setBedrockApiKey(data.bedrock_api_key || '');
                setAwsRegion(data.aws_region || 'us-east-1');
                setSqlConnectionString(data.sql_connection_string || '');
                setN8nUrl(data.n8n_url || 'http://localhost:5678');
                setN8nApiKey(data.n8n_api_key || '');
                setVaultEnabled(data.vault_enabled !== undefined ? data.vault_enabled : true);
                setVaultThreshold(data.vault_threshold || 100000);
                setAutoCompactEnabled(data.auto_compact_enabled || false);
                setAutoCompactThreshold(data.auto_compact_threshold || 50000);
                setAutoCompactWindowPct(typeof data.auto_compact_window_pct === 'number' ? data.auto_compact_window_pct : 50);
                setAllowDbWrite(data.allow_db_write || false);
                setKnowledgeEnabled(!!data.knowledge_enabled);
                setBoards({
                    enabled: !!data.boards_enabled,
                    maxOpenCards: Number(data.boards_max_open_cards) || 2000,
                    maxShared: Number(data.boards_max_shared) || 0,
                });
                setTeamCeiling(Math.max(1, Number(data.team_at_once_ceiling) || 10));
                setTurnLimitMode(typeof data.turn_limit_mode === 'string' ? data.turn_limit_mode : '');
                setToolPolicyDefaults(data.tool_policy_defaults || {});
                setEmbedCode(data.embed_code || false);
                setBashAllowedDirs(data.bash_allowed_dirs || []);
                setLoginEnabled(data.login_enabled || false);
                setLoginUsername(data.login_username || '');
            });

        // Personal details
        fetch('/api/personal-details')
            .then(res => res.json())
            .then(data => {
                setPdFirstName(data.first_name || '');
                setPdLastName(data.last_name || '');
                setPdEmail(data.email || '');
                setPdPhone(data.phone_number || '');
                const addr = data.address || {};
                setPdAddress1(addr.address1 || '');
                setPdAddress2(addr.address2 || '');
                setPdCity(addr.city || '');
                setPdState(addr.state || '');
                setPdZipcode(addr.zipcode || '');
            })
            .catch(() => { });

        refreshModels();

    }, [initialized, rModels, dispatch]);

    // Refresh Bedrock models dynamically when switching into bedrock mode.
    useEffect(() => {
        if (mode !== 'bedrock') return;

        refreshBedrockModels();
    }, [mode]);

    // Filter models based on mode
    const filteredModels = mode === 'local'
        ? localModels
        : (mode === 'bedrock' ? cloudModels.filter(m => m.startsWith('bedrock')) : cloudModels.filter(m => !m.startsWith('bedrock')));

    // Title and blurb come from the nav module, which is also what drives the
    // rail and the settings sub-nav. This replaces
    //   `Manage your agent's {activeTab} configuration.`
    // which rendered for twenty different screens, "api_keys" included.
    //
    // The fallback is unreachable from the route, which redirects a tab with no
    // entry — and that is the point: a tab reachable only by URL still needs an
    // entry of its own, or it borrows this one's title and blurb. `datalab` did,
    // and announced itself as "General" (F45). A tab rendered below without an
    // entry above lands here, so it is a fallback and not a default.
    const nav = navEntryFor(activeTab) ?? navEntryFor('general')!;

    return (
        <SavedSecretsContext.Provider value={{ secrets: secretState, remove: removeSecret, save: saveSecret }}>
            <Screen
                eyebrow="Settings"
                width="wide"
                title={nav.label}
                description={nav.blurb}
                aside={<SettingsNav />}
                /* The close control is rendered here rather than being a prop
                   on `Screen`: leaving the section is a fact about Settings,
                   not about page frames in general, and the kit's Screen is
                   shared with a product whose settings has no such exit. */
                actions={isSettingsEntry(activeTab) ? (
                    <LinkButton
                        href="/"
                        variant="ghost"
                        iconOnly
                        aria-label="Close settings"
                        title="Close settings"
                    >
                        <X className="size-4" aria-hidden />
                    </LinkButton>
                ) : undefined}
            >
                {/* One column for every tab (M2g, Naveen: sections the same length): its rules, heads and
                    actions line up with the fields under them. The frame is wide; the column is the form's. */}
                <div className="max-w-[48rem]">
                {/* IMPORT / EXPORT TAB */}
                {activeTab === 'import_export' && (
                    <ImportExportTab
                        defaultView={initialSubTab === 'examples' ? 'examples' : undefined}
                        onImportSuccess={() => dispatch(fetchAllSettingsData())}
                        // Used to call setActiveTab, which swapped the content
                        // without changing the URL.
                        onNavigate={(tab) => router.push(navEntryFor(tab)?.href ?? `/settings/${tab}`)}
                    />
                )}

                {/* GENERAL TAB */}
                {activeTab === 'general' && (
                    <GeneralTab
                        agentName={agentName}
                        setAgentName={setAgentName}
                        vaultEnabled={vaultEnabled}
                        setVaultEnabled={setVaultEnabled}
                        vaultThreshold={vaultThreshold}
                        setVaultThreshold={setVaultThreshold}
                        autoCompactEnabled={autoCompactEnabled}
                        setAutoCompactEnabled={setAutoCompactEnabled}
                        autoCompactThreshold={autoCompactThreshold}
                        setAutoCompactThreshold={setAutoCompactThreshold}
                        autoCompactWindowPct={autoCompactWindowPct}
                        setAutoCompactWindowPct={setAutoCompactWindowPct}
                        allowDbWrite={allowDbWrite}
                        setAllowDbWrite={setAllowDbWrite}
                        knowledgeEnabled={knowledgeEnabled}
                        setKnowledgeEnabled={setKnowledgeEnabled}
                        boards={boards}
                        setBoards={setBoards}
                        teamCeiling={teamCeiling}
                        setTeamCeiling={setTeamCeiling}
                        turnLimitMode={turnLimitMode}
                        setTurnLimitMode={setTurnLimitMode}
                        toolPolicyDefaults={toolPolicyDefaults}
                        setToolPolicyDefaults={setToolPolicyDefaults}
                        embedCode={embedCode}
                        setEmbedCode={setEmbedCode}
                        bashAllowedDirs={bashAllowedDirs}
                        setBashAllowedDirs={setBashAllowedDirs}
                        transformRuntime={transformRuntime}
                        loginEnabled={loginEnabled}
                        loginUsername={loginUsername}
                        setLoginUsername={setLoginUsername}
                        onSaveLogin={handleSaveLogin}
                        isLoginSaving={isLoginSaving}
                        onSave={handleSaveSection}
                        isSaving={isSaving}
                    />
                )}

                {/* PERSONAL DETAILS TAB */}
                {activeTab === 'variables' && <VariablesTab />}
                {activeTab === 'personal_details' && (
                    <PersonalDetailsTab
                        pdFirstName={pdFirstName} setPdFirstName={setPdFirstName}
                        pdLastName={pdLastName} setPdLastName={setPdLastName}
                        pdEmail={pdEmail} setPdEmail={setPdEmail}
                        pdPhone={pdPhone} setPdPhone={setPdPhone}
                        pdAddress1={pdAddress1} setPdAddress1={setPdAddress1}
                        pdAddress2={pdAddress2} setPdAddress2={setPdAddress2}
                        pdCity={pdCity} setPdCity={setPdCity}
                        pdState={pdState} setPdState={setPdState}
                        pdZipcode={pdZipcode} setPdZipcode={setPdZipcode}
                        onSave={handleSavePersonalDetails}
                    />
                )}

                {/* DATA LAB TAB */}
                {activeTab === 'datalab' && (
                    <DataLabTab
                        dlTopic={dlTopic} setDlTopic={setDlTopic}
                        dlCount={dlCount} setDlCount={setDlCount}
                        dlModel={dlModel} setDlModel={setDlModel}
                        modelOptions={Object.values(providers).flatMap(p => (p.available ? p.models : []))}
                        defaultModel={selectedModel}
                        dlSystemPrompt={dlSystemPrompt} setDlSystemPrompt={setDlSystemPrompt}
                        dlEdgeCases={dlEdgeCases} setDlEdgeCases={setDlEdgeCases}
                        dlStatus={dlStatus}
                        dlDatasets={dlDatasets}
                        onGenerate={handleGenerateData}
                    />
                )}

                {/* MODELS TAB */}
                {activeTab === 'models' && (
                    <ModelsTab
                        providers={providers}
                        mode={mode} setMode={setMode}
                        selectedModel={selectedModel} setSelectedModel={setSelectedModel}
                        embeddingModel={embeddingModel} setEmbeddingModel={setEmbeddingModel}
                        builderModel={builderModel} setBuilderModel={setBuilderModel}
                        builderQuickModel={builderQuickModel} setBuilderQuickModel={setBuilderQuickModel}
                        maxOutputTokens={maxOutputTokens} setMaxOutputTokens={setMaxOutputTokens}
                        reasoningEffort={reasoningEffort} setReasoningEffort={setReasoningEffort}
                        reasoningWithoutOff={rModels.reasoning_without_off || []}
                        reasoningOffMinimum={rModels.reasoning_off_minimum || []}
                        localModels={localModels} cloudModels={cloudModels}
                        filteredModels={filteredModels}
                        loadingModels={loadingModels}
                        onRefresh={() => void refreshModels(true)}
                        rerankListings={rerankListings}
                        awsRegion={awsRegion} setAwsRegion={setAwsRegion}
                        onSave={handleSaveSection}
                        isSaving={isSaving}
                        openaiCompatibleBaseUrl={openaiCompatibleBaseUrl} setOpenaiCompatibleBaseUrl={setOpenaiCompatibleBaseUrl}
                        openaiCompatibleModels={openaiCompatibleModels} setOpenaiCompatibleModels={setOpenaiCompatibleModels}
                        openaiCompatibleEmbedModels={openaiCompatibleEmbedModels} setOpenaiCompatibleEmbedModels={setOpenaiCompatibleEmbedModels}
                        localCompatibleBaseUrl={localCompatibleBaseUrl} setLocalCompatibleBaseUrl={setLocalCompatibleBaseUrl}
                        localCompatibleModels={localCompatibleModels} setLocalCompatibleModels={setLocalCompatibleModels}
                        localCompatibleEmbedModels={localCompatibleEmbedModels} setLocalCompatibleEmbedModels={setLocalCompatibleEmbedModels}
                        huggingfaceModels={huggingfaceModels} setHuggingfaceModels={setHuggingfaceModels}
                        anthropicCliModels={anthropicCliModels} setAnthropicCliModels={setAnthropicCliModels}
                        geminiCliModels={geminiCliModels} setGeminiCliModels={setGeminiCliModels}
                        codexCliModels={codexCliModels} setCodexCliModels={setCodexCliModels}
                        githubCopilotCliModels={githubCopilotCliModels} setGithubCopilotCliModels={setGithubCopilotCliModels}
                        knowledgeModels={knowledgeModels} setKnowledgeModels={setKnowledgeModels}
                    />
                )}

                {/* INTEGRATIONS TAB */}
                {activeTab === 'workspace' && (
                    <IntegrationsTab
                        n8nUrl={n8nUrl} setN8nUrl={setN8nUrl}
                        n8nApiKey={n8nApiKey} setN8nApiKey={setN8nApiKey}
                        onSave={handleSaveSection}
                    />
                )}

                {/* MEMORY TAB */}
                {activeTab === 'memory' && (
                    <MemoryTab />
                )}

                {/* REPOS TAB */}
                {activeTab === 'repos' && (
                    <ReposTab embeddingModel={embeddingModel} embedCode={embedCode} />
                )}

                {/* DB CONFIGS TAB */}
                {activeTab === 'db_configs' && (
                    <DBsTab />
                )}


                {/* API KEYS TAB */}
                {activeTab === 'mcp_server' && <McpServerTab />}
                {activeTab === 'api_keys' && (
                    <APIKeysTab />
                )}

                {/* SCALE TAB */}
                {activeTab === 'scale' && (
                    <ScaleTab />
                )}


                {/* SUPPORT TAB */}
                {activeTab === 'support' && <SupportTab />}
                </div>
            </Screen>

            {/* Toast Notification */}
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}

        </SavedSecretsContext.Provider>
    );
};
