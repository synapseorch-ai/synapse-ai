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
import { isSettingsEntry, navEntryFor } from '@/lib/nav';
import { GeneralTab } from './settings/GeneralTab';
import type { ToolPolicy } from './settings/ToolPolicy';
import { PersonalDetailsTab } from './settings/PersonalDetailsTab';
import { VariablesTab } from './settings/VariablesTab';
import { MemoryTab } from './settings/MemoryTab';
import { DataLabTab } from './settings/DataLabTab';
import { ModelsTab } from './settings/ModelsTab';
import { IntegrationsTab } from './settings/IntegrationsTab';
import { ToastNotification } from './settings/ToastNotification';
import { ReposTab } from './settings/ReposTab';
import { DBsTab } from './settings/DBsTab';
import { MessagingTab } from './settings/MessagingTab';
import { WidgetsTab } from './settings/WidgetsTab';
import { ImportExportTab } from './settings/ImportExportTab';
import { SupportTab } from './settings/SupportTab';
import { APIKeysTab } from './settings/APIKeysTab';
import { ScaleTab } from './settings/ScaleTab';


export const SettingsView = ({ initialTab = 'general', initialSubTab }: { initialTab?: string; initialSubTab?: string }) => {
    const dispatch = useDispatch<AppDispatch>();
    const { models: rModels, initialized } = useSelector((state: RootState) => state.settings);

    const activeTab = initialTab as Tab;
    const [agentName, setAgentName] = useState('');
    const [selectedModel, setSelectedModel] = useState('');
    const [embeddingModel, setEmbeddingModel] = useState('');
    const [builderModel, setBuilderModel] = useState('');
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
    const [allowDbWrite, setAllowDbWrite] = useState(false);
    const [toolPolicyDefaults, setToolPolicyDefaults] = useState<ToolPolicy>({});
    const [bashAllowedDirs, setBashAllowedDirs] = useState<string[]>([]);

    // Keys
    const [openaiKey, setOpenaiKey] = useState('');
    const [anthropicKey, setAnthropicKey] = useState('');
    const [geminiKey, setGeminiKey] = useState('');
    const [grokKey, setGrokKey] = useState('');
    const [deepseekKey, setDeepseekKey] = useState('');
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

    const refreshModels = async () => {
        setLoadingModels(true);
        try {
            const res = await fetch('/api/models');
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

    const handleSaveSection = async () => {
        setIsSaving(true);
        const payload = {
            agent_name: agentName,
            model: selectedModel,
            embedding_model: embeddingModel,
            builder_model: builderModel,
            mode: mode,
            openai_key: openaiKey,
            anthropic_key: anthropicKey,
            gemini_key: geminiKey,
            grok_key: grokKey,
            deepseek_key: deepseekKey,
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
            transform_runtime: transformRuntime,
            bedrock_api_key: bedrockApiKey,
            aws_region: awsRegion,
            sql_connection_string: sqlConnectionString,
            n8n_url: n8nUrl,
            n8n_api_key: n8nApiKey,
            vault_enabled: vaultEnabled,
            vault_threshold: vaultThreshold,
            auto_compact_enabled: autoCompactEnabled,
            auto_compact_threshold: autoCompactThreshold,
            allow_db_write: allowDbWrite,
            tool_policy_defaults: toolPolicyDefaults,
            embed_code: embedCode,
            bash_allowed_dirs: bashAllowedDirs,
        };

        try {
            const response = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!response.ok) throw new Error('Failed to update settings');

            if (mode === 'bedrock' || bedrockApiKey) {
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

    const handleSaveLogin = async (enabled: boolean, username: string, password: string) => {
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
            showToast(enabled ? 'Login enabled' : 'Login disabled', 'success');
        } catch (e: any) {
            showToast(e.message || 'Failed to save login settings', 'error');
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
                setMode(data.mode || 'local');
                setOpenaiKey(data.openai_key || '');
                setAnthropicKey(data.anthropic_key || '');
                setGeminiKey(data.gemini_key || '');
                setGrokKey(data.grok_key || '');
                setDeepseekKey(data.deepseek_key || '');
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
                setAllowDbWrite(data.allow_db_write || false);
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
        <>
            <Screen
                title={nav.label}
                description={nav.blurb}
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
                {/* MESSAGING TAB */}
                {activeTab === 'messaging' && <MessagingTab />}
                {activeTab === 'widgets' && <WidgetsTab />}

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
                        allowDbWrite={allowDbWrite}
                        setAllowDbWrite={setAllowDbWrite}
                        toolPolicyDefaults={toolPolicyDefaults}
                        setToolPolicyDefaults={setToolPolicyDefaults}
                        embedCode={embedCode}
                        setEmbedCode={setEmbedCode}
                        bashAllowedDirs={bashAllowedDirs}
                        setBashAllowedDirs={setBashAllowedDirs}
                        transformRuntime={transformRuntime}
                        setTransformRuntime={setTransformRuntime}
                        loginEnabled={loginEnabled}
                        setLoginEnabled={setLoginEnabled}
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
                        localModels={localModels} cloudModels={cloudModels}
                        filteredModels={filteredModels}
                        loadingModels={loadingModels}
                        openaiKey={openaiKey} setOpenaiKey={setOpenaiKey}
                        anthropicKey={anthropicKey} setAnthropicKey={setAnthropicKey}
                        geminiKey={geminiKey} setGeminiKey={setGeminiKey}
                        grokKey={grokKey} setGrokKey={setGrokKey}
                        deepseekKey={deepseekKey} setDeepseekKey={setDeepseekKey}
                        bedrockApiKey={bedrockApiKey} setBedrockApiKey={setBedrockApiKey}
                        awsRegion={awsRegion} setAwsRegion={setAwsRegion}
                        onSave={handleSaveSection}
                        isSaving={isSaving}
                        openaiCompatibleKey={openaiCompatibleKey} setOpenaiCompatibleKey={setOpenaiCompatibleKey}
                        openaiCompatibleBaseUrl={openaiCompatibleBaseUrl} setOpenaiCompatibleBaseUrl={setOpenaiCompatibleBaseUrl}
                        openaiCompatibleModels={openaiCompatibleModels} setOpenaiCompatibleModels={setOpenaiCompatibleModels}
                        openaiCompatibleEmbedModels={openaiCompatibleEmbedModels} setOpenaiCompatibleEmbedModels={setOpenaiCompatibleEmbedModels}
                        localCompatibleBaseUrl={localCompatibleBaseUrl} setLocalCompatibleBaseUrl={setLocalCompatibleBaseUrl}
                        localCompatibleKey={localCompatibleKey} setLocalCompatibleKey={setLocalCompatibleKey}
                        localCompatibleModels={localCompatibleModels} setLocalCompatibleModels={setLocalCompatibleModels}
                        localCompatibleEmbedModels={localCompatibleEmbedModels} setLocalCompatibleEmbedModels={setLocalCompatibleEmbedModels}
                        huggingfaceToken={huggingfaceToken} setHuggingfaceToken={setHuggingfaceToken}
                        huggingfaceModels={huggingfaceModels} setHuggingfaceModels={setHuggingfaceModels}
                        anthropicCliModels={anthropicCliModels} setAnthropicCliModels={setAnthropicCliModels}
                        geminiCliModels={geminiCliModels} setGeminiCliModels={setGeminiCliModels}
                        codexCliModels={codexCliModels} setCodexCliModels={setCodexCliModels}
                        githubCopilotCliModels={githubCopilotCliModels} setGithubCopilotCliModels={setGithubCopilotCliModels}
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
                {activeTab === 'api_keys' && (
                    <APIKeysTab />
                )}

                {/* SCALE TAB */}
                {activeTab === 'scale' && (
                    <ScaleTab />
                )}


                {/* SUPPORT TAB */}
                {activeTab === 'support' && <SupportTab />}
            </Screen>

            {/* Toast Notification */}
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}

        </>
    );
};
