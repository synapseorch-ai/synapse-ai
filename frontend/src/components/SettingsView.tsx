"use client";
/* eslint-disable @typescript-eslint/ban-ts-comment */
/* eslint-disable react/no-unescaped-entities */
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import { Settings, X, Shield, Trash, Cpu, Cloud, Database, LayoutGrid, Bot, Wrench, Server, FolderGit2, Workflow, ScrollText, MessageSquare, Clock, ArrowLeftRight, Vault, LifeBuoy, Key } from 'lucide-react';

import { useRouter } from 'next/navigation';
import { useDispatch, useSelector } from 'react-redux';
import { RootState, AppDispatch } from '@/store';
import { fetchAllSettingsData, removeAgent, removeMcpServer, removeCustomTool, updateCustomTool, addCustomTool, addMcpServer, updateMcpServerStatus } from '@/store/settingsSlice';

import type { Tab } from './settings/types';
import { GeneralTab } from './settings/GeneralTab';
import { PersonalDetailsTab } from './settings/PersonalDetailsTab';
import { MemoryTab } from './settings/MemoryTab';
import { AgentsTab } from './settings/AgentsTab';
import { CustomToolsTab } from './settings/CustomToolsTab';
import { DataLabTab } from './settings/DataLabTab';
import { ModelsTab } from './settings/ModelsTab';
import { IntegrationsTab } from './settings/IntegrationsTab';
import { McpServersTab } from './settings/McpServersTab';
import { ConfirmationModal } from './settings/ConfirmationModal';
import { ToastNotification } from './settings/ToastNotification';
import { ReposTab } from './settings/ReposTab';
import { DBsTab } from './settings/DBsTab';
import { OrchestrationTab } from './settings/OrchestrationTab';
import { LogsTab } from './settings/LogsTab';
import { MessagingTab } from './settings/MessagingTab';
import { UsageTab } from './settings/UsageTab';
import { SchedulesTab } from './settings/SchedulesTab';
import { ImportExportTab } from './settings/ImportExportTab';
import { VaultTab } from './settings/VaultTab';
import { SupportTab } from './settings/SupportTab';
import { APIKeysTab } from './settings/APIKeysTab';


export const SettingsView = ({ initialTab = 'general', initialSubTab }: { initialTab?: string; initialSubTab?: string }) => {
    const dispatch = useDispatch<AppDispatch>();
    const { agents, mcpServers, customTools, models: rModels, initialized, loading: loadingAgents } = useSelector((state: RootState) => state.settings);

    const [activeTab, setActiveTab] = useState<Tab>(initialTab as Tab);
    const [agentName, setAgentName] = useState('');
    const [selectedModel, setSelectedModel] = useState('');
    const [embeddingModel, setEmbeddingModel] = useState('');
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
    const [awsRegion, setAwsRegion] = useState('us-east-1');
    const [bedrockInferenceProfile, setBedrockInferenceProfile] = useState('');
    const [bedrockInferenceProfiles, setBedrockInferenceProfiles] = useState<Array<{ id: string; arn: string; name: string; status?: string; type?: string }>>([]);
    const [loadingInferenceProfiles, setLoadingInferenceProfiles] = useState(false);
    const [inferenceProfilesError, setInferenceProfilesError] = useState<string | null>(null);
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

    // Agents State
    const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
    const [draftAgent, setDraftAgent] = useState<any>(null);


    // Custom Tools State
    const [draftTool, setDraftTool] = useState<any>(null);
    const [toolBuilderMode, setToolBuilderMode] = useState<'config' | 'python'>('config');
    const [headerRows, setHeaderRows] = useState<{ id: string, key: string, value: string }[]>([]);
    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'warning' | 'error' } | null>(null);
    const showToast = (message: string, type: 'success' | 'warning' | 'error' = 'success') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), 4000);
    };

    // n8n workflows (for Tool Builder dropdown)
    const [n8nWorkflows, setN8nWorkflows] = useState<any[]>([]);
    const [n8nWorkflowsLoading, setN8nWorkflowsLoading] = useState(false);

    // MCP Servers State
    const [loadingMcp, setLoadingMcp] = useState(false);
    const [isConnectingMcp, setIsConnectingMcp] = useState(false);
    const [lastMcpConnected, setLastMcpConnected] = useState<boolean | null>(null);
    const [mcpToast, setMcpToast] = useState<{ show: boolean; message: string; type: 'success' | 'warning' | 'error' } | null>(null);
    const [pendingMcpServerName, setPendingMcpServerName] = useState<string | null>(null);
    const [draftMcpServer, setDraftMcpServer] = useState<{
        name: string; label: string; server_type: 'stdio' | 'remote';
        command: string; args: string; env: { key: string; value: string }[];
        url: string; token: string;
    }>({ name: '', label: '', server_type: 'stdio', command: '', args: '', env: [], url: '', token: '' });

    const [availableCapabilities, setAvailableCapabilities] = useState<any[]>([]);
    const [loadingCapabilities, setLoadingCapabilities] = useState(true);
    const [messagingEnabled, setMessagingEnabled] = useState(false);
    const [codingEnabled, setCodingEnabled] = useState(false);
    const [embedCode, setEmbedCode] = useState(false);

    // Login settings
    const [loginEnabled, setLoginEnabled] = useState(false);
    const [loginUsername, setLoginUsername] = useState('');
    const [isLoginSaving, setIsLoginSaving] = useState(false);

    // Persistent OAuth postMessage listener — lives here so it survives settings tab switches
    const handleMcpOAuthMessage = useCallback((event: MessageEvent) => {
        if (event.data?.type !== 'MCP_OAUTH_COMPLETE') return;
        if (event.data.success) {
            const name = event.data.name as string;
            dispatch(updateMcpServerStatus({ name, status: 'connected' }));
            setMcpToast({ show: true, message: `✓ ${name} connected via OAuth!`, type: 'success' });
            setPendingMcpServerName(null);
            setTimeout(() => setMcpToast(null), 5000);
        } else {
            setMcpToast({ show: true, message: `OAuth failed: ${event.data.error}`, type: 'error' });
            setTimeout(() => setMcpToast(null), 6000);
        }
    }, [dispatch]);

    useEffect(() => {
        window.addEventListener('message', handleMcpOAuthMessage);
        return () => window.removeEventListener('message', handleMcpOAuthMessage);
    }, [handleMcpOAuthMessage]);

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

    const refreshBedrockInferenceProfiles = async () => {
        setLoadingInferenceProfiles(true);
        setInferenceProfilesError(null);
        try {
            const res = await fetch('/api/bedrock/inference-profiles');
            const data = await res.json();
            const profiles = Array.isArray(data.profiles) ? data.profiles : [];
            setBedrockInferenceProfiles(profiles);
            if (data.error) setInferenceProfilesError(data.error);
        } catch {
            setBedrockInferenceProfiles([]);
            setInferenceProfilesError('Failed to reach the server.');
        } finally {
            setLoadingInferenceProfiles(false);
        }
    };

    const handleSaveSection = async () => {
        setIsSaving(true);
        const payload = {
            agent_name: agentName,
            model: selectedModel,
            embedding_model: embeddingModel,
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
            bedrock_api_key: bedrockApiKey,
            bedrock_inference_profile: bedrockInferenceProfile,
            aws_region: awsRegion,
            sql_connection_string: sqlConnectionString,
            n8n_url: n8nUrl,
            n8n_api_key: n8nApiKey,
            vault_enabled: vaultEnabled,
            vault_threshold: vaultThreshold,
            auto_compact_enabled: autoCompactEnabled,
            auto_compact_threshold: autoCompactThreshold,
            allow_db_write: allowDbWrite,
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
                await refreshBedrockInferenceProfiles();
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

    const [n8nWorkflowId, setN8nWorkflowId] = useState<string | null>(null);

    // Confirmation Modal State
    const [confirmAction, setConfirmAction] = useState<{
        type: 'history_recent' | 'history_all' | 'delete_mcp' | 'delete_tool' | 'delete_agent',
        message: string,
        payload?: any
    } | null>(null);

    // Data Lab State
    const [dlTopic, setDlTopic] = useState('');
    const [dlCount, setDlCount] = useState(10);
    const [dlProvider, setDlProvider] = useState('openai');
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
            const interval = setInterval(() => {
                fetchStatus();
                if (dlStatus?.status === 'generating') fetchDatasets(); // Refresh list occasionally
            }, 2000);
            return () => clearInterval(interval);
        }
    }, [activeTab]);

    const fetchDatasets = () => fetch('/api/synthetic/datasets').then(r => r.json()).then(setDlDatasets).catch(() => { });
    const fetchStatus = () => fetch('/api/synthetic/status').then(r => r.json()).then(setDlStatus).catch(() => { });

    const getN8nBaseUrl = () => (n8nUrl || 'http://localhost:5678').replace(/\/+$/, '');

    const handleGenerateData = async () => {
        if (!dlTopic) { showToast('Please enter a topic', 'warning'); return; }
        if (dlProvider === 'openai' && !openaiKey) { showToast('OpenAI Key required', 'warning'); return; }
        if (dlProvider === 'gemini' && !geminiKey) { showToast('Gemini Key required', 'warning'); return; }

        try {
            const res = await fetch('/api/synthetic/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    topic: dlTopic,
                    count: dlCount,
                    provider: dlProvider,
                    api_key: dlProvider === 'openai' ? openaiKey : geminiKey,
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

    // Actual Execution
    const executeConfirmAction = async () => {
        if (!confirmAction) return;

        try {
            if (confirmAction.type === 'delete_mcp') {
                const res = await fetch(`/api/mcp/servers/${confirmAction.payload}`, { method: 'DELETE' });
                if (res.ok) dispatch(removeMcpServer(confirmAction.payload));
            } else if (confirmAction.type === 'delete_tool') {
                await fetch(`/api/tools/custom/${confirmAction.payload}`, { method: 'DELETE' });
                dispatch(removeCustomTool(confirmAction.payload));
            } else if (confirmAction.type === 'delete_agent') {
                await fetch(`/api/agents/${confirmAction.payload}`, { method: 'DELETE' });
                dispatch(removeAgent(confirmAction.payload));
                if (selectedAgentId === confirmAction.payload) {
                    setSelectedAgentId(null);
                    setDraftAgent(null);
                }
            }
        } catch (e) {
            showToast('Error running confirmation action', 'error');
        } finally {
            setConfirmAction(null);
        }
    };

    // Close on escape
    useEffect(() => {
        const handleEsc = (e: KeyboardEvent) => {
            if (e.key === 'Escape') router.push('/');
        };
        document.addEventListener('keydown', handleEsc);
        return () => document.removeEventListener('keydown', handleEsc);
    }, [router]);
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
                setBedrockApiKey(data.bedrock_api_key || '');
                setAwsRegion(data.aws_region || 'us-east-1');
                setBedrockInferenceProfile(data.bedrock_inference_profile || '');
                setSqlConnectionString(data.sql_connection_string || '');
                setN8nUrl(data.n8n_url || 'http://localhost:5678');
                setN8nApiKey(data.n8n_api_key || '');
                setVaultEnabled(data.vault_enabled !== undefined ? data.vault_enabled : true);
                setVaultThreshold(data.vault_threshold || 100000);
                setAutoCompactEnabled(data.auto_compact_enabled || false);
                setAutoCompactThreshold(data.auto_compact_threshold || 50000);
                setAllowDbWrite(data.allow_db_write || false);
                setEmbedCode(data.embed_code || false);
                setBashAllowedDirs(data.bash_allowed_dirs || []);
                setMessagingEnabled(data.messaging_enabled || false);
                setCodingEnabled(data.coding_agent_enabled || false);
                setLoginEnabled(data.login_enabled || false);
                setLoginUsername(data.login_username || '');
                if (data.bedrock_api_key) {
                    refreshBedrockInferenceProfiles();
                }
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

        // Get Available Capabilities (Dynamic Tools + MCP)
        setLoadingCapabilities(true);
        fetch('/api/tools/available')
            .then(res => res.json())
            .then(data => {
                const tools = data.tools || [];
                const groups: Record<string, any> = {};

                tools.forEach((t: any) => {
                    // Special handling for legacy custom tools: UNGROUP THEM
                    if (t.source === 'custom_http') {
                        const capId = t.name;
                        // avoid duplicate if same custom tool appears somehow
                        if (!groups[capId]) {
                            groups[capId] = {
                                id: capId,
                                label: t.label || t.name, // Use generalName if available
                                description: t.description,
                                tools: [t.name],
                                toolDetails: [{ name: t.name, description: t.description || '' }],
                                toolType: 'custom'
                            };
                        }
                    } else {
                        // Group by source (e.g., 'gmail', 'filesystem')
                        const source = t.source || 'unknown';
                        if (!groups[source]) {
                            groups[source] = {
                                id: source,
                                label: t.source_label || source.charAt(0).toUpperCase() + source.slice(1).replace(/_/g, ' '),
                                description: `Tools from ${source}`,
                                tools: [],
                                toolDetails: [],
                                /*
                                 * Determine tool type for badge:
                                 * - mcp_external -> 'mcp'
                                 * - mcp_native -> 'native' (no badge, but logic might say otherwise)
                                 * - custom_http -> 'custom' (handled above really, but safe fallback)
                                 */
                                toolType: t.type === 'mcp_external' ? 'mcp' : (t.type === 'mcp_native' ? 'native' : 'custom')
                            };
                        }
                        groups[source].tools.push(t.name);
                        groups[source].toolDetails.push({ name: t.name, description: t.description || '' });
                    }
                });

                const dynamicCaps = Object.values(groups);
                setAvailableCapabilities(dynamicCaps);
            })
            .finally(() => setLoadingCapabilities(false));
    }, [initialized, rModels, dispatch]);

    // Refresh Bedrock models dynamically when switching into bedrock mode.
    useEffect(() => {
        if (mode !== 'bedrock') return;

        refreshBedrockModels();
        refreshBedrockInferenceProfiles();
    }, [mode]);

    // Fetch n8n workflows when the Tool Builder is open (for dropdown)
    useEffect(() => {
        if (activeTab !== 'custom_tools') return;
        if (!draftTool) return;
        if (toolBuilderMode !== 'config') return;
        if (n8nWorkflows.length > 0) return;
        fetchN8nWorkflows();
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [activeTab, draftTool, toolBuilderMode]);

    // Fetch MCP Servers
    useEffect(() => {
        if (activeTab === 'mcp_servers' && !initialized) {
            dispatch(fetchAllSettingsData());
        }
    }, [activeTab, dispatch, initialized]);

    const handleAddMcpServer = async () => {
        if (!draftMcpServer.name) {
            setMcpToast({ show: true, message: 'Server name is required.', type: 'error' });
            setTimeout(() => setMcpToast(null), 4000);
            return;
        }
        if (draftMcpServer.server_type === 'stdio' && !draftMcpServer.command) {
            setMcpToast({ show: true, message: 'Command is required for local servers.', type: 'error' });
            setTimeout(() => setMcpToast(null), 4000);
            return;
        }
        if (draftMcpServer.server_type === 'remote' && !draftMcpServer.url) {
            setMcpToast({ show: true, message: 'URL is required for remote servers.', type: 'error' });
            setTimeout(() => setMcpToast(null), 4000);
            return;
        }

        const argsList = draftMcpServer.args.match(/(?:[^\s"]+|"[^"]*")+/g)?.map(s => s.replace(/^"|"$/g, '')) || [];
        const envObj = draftMcpServer.env.reduce((acc, curr) => {
            if (curr.key) acc[curr.key] = curr.value;
            return acc;
        }, {} as Record<string, string>);

        setIsConnectingMcp(true);
        try {
            const res = await fetch('/api/mcp/servers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: draftMcpServer.name,
                    label: draftMcpServer.label,
                    server_type: draftMcpServer.server_type,
                    command: draftMcpServer.command,
                    args: argsList,
                    env: envObj,
                    url: draftMcpServer.url,
                    token: draftMcpServer.token,
                })
            });
            if (res.ok) {
                const data = await res.json();
                dispatch(addMcpServer(data.config));
                setDraftMcpServer({ name: '', label: '', server_type: 'stdio', command: '', args: '', env: [], url: '', token: '' });

                if (data.status === 'oauth_pending') {
                    setLastMcpConnected(false);
                    setPendingMcpServerName(draftMcpServer.name);
                    setMcpToast({ show: true, message: '🔑 OAuth required — opening browser. Return here once authorised.', type: 'warning' });
                    if (data.auth_url) window.open(data.auth_url, '_blank');
                } else if (data.connected) {
                    setLastMcpConnected(true);
                    setMcpToast({ show: true, message: '✓ Server connected and saved!', type: 'success' });
                } else {
                    setLastMcpConnected(false);
                    setMcpToast({ show: true, message: '⚠ Config saved. Use Retry to reconnect.', type: 'warning' });
                }
                setTimeout(() => setMcpToast(null), 7000);
            } else {
                const err = await res.json();
                setMcpToast({ show: true, message: `Error: ${err.detail || 'Unknown error'}`, type: 'error' });
                setTimeout(() => setMcpToast(null), 5000);
            }
        } catch {
            setMcpToast({ show: true, message: 'Failed to reach backend. Is it running?', type: 'error' });
            setTimeout(() => setMcpToast(null), 5000);
        } finally {
            setIsConnectingMcp(false);
        }
    };

    const handleReconnectMcpServer = async (name: string) => {
        try {
            dispatch(updateMcpServerStatus({ name, status: 'connecting' }));
            const res = await fetch(`/api/mcp/servers/${name}/reconnect`, { method: 'POST' });
            const data = await res.json();
            if (data.connected) {
                dispatch(updateMcpServerStatus({ name, status: 'connected' }));
                setMcpToast({ show: true, message: `✓ ${name} reconnected!`, type: 'success' });
            } else {
                dispatch(updateMcpServerStatus({ name, status: 'disconnected' }));
                setMcpToast({ show: true, message: `Could not connect to ${name}. Complete OAuth first.`, type: 'warning' });
            }
            setTimeout(() => setMcpToast(null), 5000);
        } catch {
            dispatch(updateMcpServerStatus({ name, status: 'disconnected' }));
            setMcpToast({ show: true, message: 'Reconnect failed.', type: 'error' });
            setTimeout(() => setMcpToast(null), 5000);
        }
    };

    const handleDeleteMcpServer = async (name: string) => {
        setConfirmAction({
            type: 'delete_mcp',
            message: `Are you sure you want to remove the MCP server '${name}'?`,
            payload: name
        });
    };

    // Handle Save Custom Tool
    const handleSaveTool = async () => {
        if (!draftTool) return;

        // ── Python tool save path ──────────────────────────────────
        if (draftTool.tool_type === 'python') {
            if (!draftTool.name) {
                showToast('System Name is required', 'warning');
                return;
            }
            if (!draftTool.code || !draftTool.code.trim()) {
                showToast('Python code cannot be empty', 'warning');
                return;
            }
            try {
                const payload = {
                    name: draftTool.name,
                    generalName: draftTool.generalName || draftTool.name,
                    description: draftTool.description || '',
                    tool_type: 'python',
                    code: draftTool.code,
                    inputSchema: draftTool.inputSchema || { type: 'object', properties: {} },
                    schemaParams: draftTool.schemaParams || [],
                };
                const res = await fetch('/api/tools/custom', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                if (res.ok) {
                    const savedResp = await res.json();
                    const saved = savedResp?.tool ?? savedResp;
                    dispatch(updateCustomTool(saved));
                    setDraftTool(null);
                    setToolBuilderMode('config');
                    showToast('Python tool saved successfully', 'success');
                } else {
                    showToast('Failed to save Python tool', 'error');
                }
            } catch {
                showToast('Error saving Python tool', 'error');
            }
            return;
        }

        // ── HTTP / n8n tool save path ──────────────────────────────
        if (!draftTool.name || !draftTool.url) {
            showToast('Name and URL are required', 'warning');
            return;
        }

        // Validate Schemas
        let finalInputSchema = draftTool.inputSchema;
        let finalOutputSchema = draftTool.outputSchema;

        try {
            if (typeof draftTool.inputSchemaStr === 'string') {
                finalInputSchema = JSON.parse(draftTool.inputSchemaStr);
            }
        } catch (e) {
            showToast('Invalid Input Schema JSON', 'error');
            return;
        }

        try {
            if (typeof draftTool.outputSchemaStr === 'string' && draftTool.outputSchemaStr.trim()) {
                finalOutputSchema = JSON.parse(draftTool.outputSchemaStr);
            } else if (!draftTool.outputSchemaStr || !draftTool.outputSchemaStr.trim()) {
                finalOutputSchema = undefined;
            }
        } catch (e) {
            showToast('Invalid Output Schema JSON', 'error');
            return;
        }

        try {
            // Convert header rows to object
            const headersObj: Record<string, string> = {};
            headerRows.forEach(r => {
                if (r.key.trim()) headersObj[r.key.trim()] = r.value;
            });

            const payload = {
                ...draftTool,
                inputSchema: finalInputSchema,
                outputSchema: finalOutputSchema,
                headers: headersObj
            };

            // Clean up temporary fields
            delete payload.inputSchemaStr;
            delete payload.outputSchemaStr;

            const res = await fetch('/api/tools/custom', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                const savedResp = await res.json();
                const saved = savedResp?.tool ?? savedResp;

                dispatch(updateCustomTool(saved));

                setDraftTool(null);
                setToolBuilderMode('config');
                showToast('Tool saved successfully', 'success');
            } else {
                showToast('Failed to save tool', 'error');
            }
        } catch (e) {
            showToast('Error saving tool', 'error');
        }
    };

    const fetchN8nWorkflows = async () => {
        if (n8nWorkflowsLoading) return;
        setN8nWorkflowsLoading(true);
        try {
            const res = await fetch('/api/n8n/workflows');
            if (!res.ok) {
                setN8nWorkflows([]);
                return;
            }
            const data = await res.json();
            setN8nWorkflows(Array.isArray(data) ? data : []);
        } catch {
            setN8nWorkflows([]);
        } finally {
            setN8nWorkflowsLoading(false);
        }
    };

    // Handle Delete Tool
    const handleDeleteTool = async (name: string) => {
        setConfirmAction({
            type: 'delete_tool',
            message: `Are you sure you want to delete the custom tool '${name}'?`,
            payload: name
        });
    };


    // Handle Delete Agent
    const handleDeleteAgent = async (id: string) => {
        setConfirmAction({
            type: 'delete_agent',
            message: "Are you sure you want to delete this agent? This action cannot be undone.",
            payload: id
        });
    };



    // Filter models based on mode
    const filteredModels = mode === 'local'
        ? localModels
        : (mode === 'bedrock' ? cloudModels.filter(m => m.startsWith('bedrock')) : cloudModels.filter(m => !m.startsWith('bedrock')));

    const tabs = [
        { id: 'general', label: 'General', icon: LayoutGrid },
        { id: 'personal_details', label: 'Personal Details', icon: Shield },
        { id: 'orchestrations', label: 'Orchestrations', icon: Workflow },
        { id: 'agents', label: 'Build Agents', icon: Bot },
        { id: 'mcp_servers', label: 'MCP Servers', icon: Server },
        { id: 'custom_tools', label: 'Tool Builder', icon: Wrench },
        ...(codingEnabled ? [{ id: 'repos', label: 'Repos', icon: FolderGit2 }] : []),
        ...(codingEnabled ? [{ id: 'db_configs', label: 'DB Configs', icon: Database }] : []),
        { id: 'models', label: 'Models', icon: Cpu },
        ...(messagingEnabled ? [{ id: 'messaging', label: 'Messaging', icon: MessageSquare }] : []),
        { id: 'workspace', label: 'Integrations', icon: Cloud },
        { id: 'schedules', label: 'Schedules', icon: Clock },
        { id: 'memory', label: 'Memory', icon: Trash },
        { id: 'logs', label: 'Logs', icon: ScrollText },
        { id: 'import_export', label: 'Import / Export', icon: ArrowLeftRight },
        { id: 'vault', label: 'Vault', icon: Vault },
        { id: 'api_keys', label: 'API Keys', icon: Key },
        { id: 'support', label: 'Support & Docs', icon: LifeBuoy },
    ];

    return (

        <div className="flex-1 flex flex-col h-full overflow-hidden bg-transparent">
            {/* Orchestrations tab: full-bleed layout, no scroll wrapper */}
            {activeTab === 'orchestrations' && (
                <div className="flex-1 flex flex-col overflow-hidden">
                    <OrchestrationTab />
                </div>
            )}

            {/* Logs tab: full-bleed two-pane layout */}
            {activeTab === 'logs' && (
                <div className="flex-1 flex flex-col overflow-hidden">
                    <LogsTab />
                </div>
            )}

            {/* Messaging tab */}
            {activeTab === 'messaging' && (
                <div className="flex-1 overflow-y-auto p-6 md:p-12">
                    <div className="max-w-5xl mx-auto animate-in fade-in slide-in-from-bottom-4 duration-300">
                        <div className="mb-8">
                            <h1 className="text-3xl font-bold mb-2 text-zinc-50">Messaging</h1>
                            <p className="text-zinc-500 text-sm">Connect your agents to Telegram, Discord, Slack, Teams, or WhatsApp.</p>
                        </div>
                        <MessagingTab />
                    </div>
                </div>
            )}

            {/* Usage tab: full-bleed analytics dashboard */}
            {activeTab === 'usage' && (
                <div className="flex-1 flex flex-col overflow-hidden">
                    <UsageTab />
                </div>
            )}

            {/* Schedules tab: full-bleed layout */}
            {activeTab === 'schedules' && (
                <div className="flex-1 flex flex-col overflow-hidden">
                    <SchedulesTab />
                </div>
            )}

            {/* Import/Export tab: scrollable layout */}
            {activeTab === 'import_export' && (
                <div className="flex-1 overflow-y-auto p-6 md:p-12">
                    <div className="max-w-5xl mx-auto animate-in fade-in slide-in-from-bottom-4 duration-300">
                        <div className="mb-8">
                            <h1 className="text-3xl font-bold mb-2 text-zinc-50">Import / Export</h1>
                            <p className="text-zinc-500 text-sm">Export your orchestrations, agents, MCP servers, and tools as a portable bundle, or import one from another Synapse instance.</p>
                        </div>
                        <ImportExportTab defaultView={initialSubTab === 'examples' ? 'examples' : undefined} onImportSuccess={() => dispatch(fetchAllSettingsData())} onNavigate={(tab) => setActiveTab(tab as Tab)} />
                    </div>
                </div>
            )}

            {/* Vault tab: full-bleed file explorer */}
            {activeTab === 'vault' && (
                <div className="flex-1 flex flex-col overflow-hidden">
                    <div className="flex-shrink-0 px-6 py-4 border-b border-zinc-800">
                        <h1 className="text-xl font-bold text-zinc-50 flex items-center gap-2">
                            <Vault className="h-5 w-5 text-zinc-400" /> Vault
                        </h1>
                        <p className="text-zinc-500 text-xs mt-1">Manage knowledge files, skills, and other files (.md, .json, .txt) that agents can reference in their prompts using @[path] syntax.</p>
                    </div>
                    <VaultTab />
                </div>
            )}

            <div className={`flex-1 overflow-y-auto p-6 md:p-12 ${activeTab === 'orchestrations' || activeTab === 'logs' || activeTab === 'messaging' || activeTab === 'usage' || activeTab === 'schedules' || activeTab === 'import_export' || activeTab === 'vault' ? 'hidden' : ''}`}>
                <div className="max-w-5xl mx-auto space-y-10 animate-in fade-in slide-in-from-bottom-4 duration-300">

                    <div className="mb-8">
                        <h1 className="text-3xl font-bold mb-2 text-zinc-50">{tabs.find(t => t.id === activeTab)?.label}</h1>
                        <p className="text-zinc-500 text-sm">Manage your agent's {activeTab} configuration.</p>
                    </div>

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
                            embedCode={embedCode}
                            setEmbedCode={setEmbedCode}
                            bashAllowedDirs={bashAllowedDirs}
                            setBashAllowedDirs={setBashAllowedDirs}
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

                    {/* AGENTS TAB */}
                    {activeTab === 'agents' && (
                        <AgentsTab
                            agents={agents.filter((a: any) => !a.id.startsWith('agent_native_builder'))}
                            selectedAgentId={selectedAgentId}
                            setSelectedAgentId={setSelectedAgentId}
                            draftAgent={draftAgent}
                            setDraftAgent={setDraftAgent}
                            availableCapabilities={availableCapabilities}
                            loadingCapabilities={loadingCapabilities}
                            customTools={customTools}
                            onDeleteAgent={handleDeleteAgent}
                            providers={providers}
                            defaultModel={selectedModel}
                            loadingAgents={loadingAgents}
                        />
                    )}



                    {/* CUSTOM TOOLS TAB */}
                    {activeTab === 'custom_tools' && (
                        <CustomToolsTab
                            customTools={customTools}
                            draftTool={draftTool}
                            setDraftTool={setDraftTool}
                            toolBuilderMode={toolBuilderMode}
                            setToolBuilderMode={setToolBuilderMode}
                            headerRows={headerRows}
                            setHeaderRows={setHeaderRows}
                            n8nWorkflows={n8nWorkflows}
                            n8nWorkflowsLoading={n8nWorkflowsLoading}
                            n8nWorkflowId={n8nWorkflowId}
                            setN8nWorkflowId={setN8nWorkflowId}
                            getN8nBaseUrl={getN8nBaseUrl}
                            onSaveTool={handleSaveTool}
                            onDeleteTool={handleDeleteTool}
                            n8nIntegrated={!!(n8nApiKey && n8nApiKey.trim())}
                        />
                    )}

                    {/* DATA LAB TAB */}
                    {activeTab === 'datalab' && (
                        <DataLabTab
                            dlTopic={dlTopic} setDlTopic={setDlTopic}
                            dlCount={dlCount} setDlCount={setDlCount}
                            dlProvider={dlProvider} setDlProvider={setDlProvider}
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
                            bedrockInferenceProfile={bedrockInferenceProfile}
                            setBedrockInferenceProfile={setBedrockInferenceProfile}
                            bedrockInferenceProfiles={bedrockInferenceProfiles}
                            loadingInferenceProfiles={loadingInferenceProfiles}
                            inferenceProfilesError={inferenceProfilesError}
                            onExpandBedrock={refreshBedrockInferenceProfiles}
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

                    {/* MCP SERVERS TAB */}
                    {activeTab === 'mcp_servers' && (
                        <McpServersTab
                            mcpServers={mcpServers}
                            loadingMcp={loadingMcp}
                            isConnecting={isConnectingMcp}
                            lastConnected={lastMcpConnected}
                            mcpToast={mcpToast}
                            setMcpToast={setMcpToast}
                            pendingServerName={pendingMcpServerName}
                            onPendingResolved={() => setPendingMcpServerName(null)}
                            draftMcpServer={draftMcpServer}
                            setDraftMcpServer={setDraftMcpServer}
                            onAddServer={handleAddMcpServer}
                            onDeleteServer={handleDeleteMcpServer}
                            onReconnectServer={handleReconnectMcpServer}
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

                    {/* SUPPORT TAB */}
                    {activeTab === 'support' && (
                        <SupportTab />
                    )}
                </div>
            </div>

            {/* Toast Notification */}
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}

            {/* Custom Confirmation Modal */}
            <ConfirmationModal
                isOpen={!!confirmAction}
                title="Confirm Action"
                message={confirmAction?.message || ""}
                onConfirm={executeConfirmAction}
                onClose={() => setConfirmAction(null)}
            />

        </div>
    );
};
