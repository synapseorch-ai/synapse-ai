/* eslint-disable @typescript-eslint/no-explicit-any */
import { Bot, Plus, Save, Trash, ChevronDown, ChevronRight, Lock, Sparkles, Eye, EyeOff, Loader2, MessageSquare, ExternalLink, CheckCircle, XCircle, Square } from 'lucide-react';
import { VaultTextarea } from '@/components/VaultMention';
import { CAPABILITIES, AUTO_TOOLS_BY_TYPE } from './types';
import { renderTextContent } from '@/lib/utils';
import { useDispatch } from 'react-redux';
import { AppDispatch } from '@/store';
import { addAgent, updateAgent } from '@/store/settingsSlice';
import { ToastNotification } from './ToastNotification';

interface AgentsTabProps {
    agents: any[];
    selectedAgentId: string | null;
    setSelectedAgentId: (id: string | null) => void;
    draftAgent: any;
    setDraftAgent: (agent: any) => void;
    availableCapabilities: any[];
    loadingCapabilities?: boolean;
    customTools: any[];
    onDeleteAgent: (id: string) => void;
    providers?: Record<string, { available: boolean; models: string[] }>;
    defaultModel?: string;
    loadingAgents?: boolean;
}

import React, { useState, useEffect } from 'react';

export const AgentsTab = ({
    agents, selectedAgentId, setSelectedAgentId,
    draftAgent, setDraftAgent, availableCapabilities, loadingCapabilities = false, customTools,
    onDeleteAgent, providers, defaultModel, loadingAgents = false
}: AgentsTabProps) => {
    const dispatch = useDispatch<AppDispatch>();
    const [repos, setRepos] = useState<any[]>([]);
    const [dbConfigs, setDbConfigs] = useState<any[]>([]);
    const [agentTypes, setAgentTypes] = useState<{ value: string; label: string; description: string }[]>([]);
    const [expandedCaps, setExpandedCaps] = useState<Set<string>>(new Set());
    const [promptDescription, setPromptDescription] = useState('');
    const [isGenerating, setIsGenerating] = useState(false);
    const [showPreview, setShowPreview] = useState(false);
    const [agentSubTab, setAgentSubTab] = useState<'config' | 'messaging'>('config');
    const [agentChannels, setAgentChannels] = useState<any[]>([]);
    const [isSaving, setIsSaving] = useState(false);
    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'error' } | null>(null);

    const showToast = (message: string, type: 'success' | 'error') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), 4000);
    };

    const handleSaveAgent = async () => {
        if (!draftAgent) return;
        setIsSaving(true);
        try {
            const res = await fetch('/api/agents', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(draftAgent),
            });
            if (res.ok) {
                const saved = await res.json();
                const isNew = !agents.some((a: any) => a.id === draftAgent.id);
                if (isNew) {
                    dispatch(addAgent(saved));
                    setSelectedAgentId(saved.id);
                    setDraftAgent(saved);
                } else {
                    dispatch(updateAgent(saved));
                }
                showToast('Agent saved successfully', 'success');
            } else {
                showToast('Failed to save agent', 'error');
            }
        } catch {
            showToast('Error saving agent', 'error');
        } finally {
            setIsSaving(false);
        }
    };

    // Reset sub-tab and re-fetch channels whenever the selected agent changes
    useEffect(() => {
        setAgentSubTab('config');
        setAgentChannels([]);
    }, [selectedAgentId]);

    useEffect(() => {
        fetch('/api/repos')
            .then(res => res.json())
            .then(data => setRepos(data))
            .catch(err => console.error("Failed to fetch repos", err));
        fetch('/api/db-configs')
            .then(res => res.json())
            .then(data => setDbConfigs(data))
            .catch(err => console.error("Failed to fetch DB configs", err));
        fetch('/api/agent-types')
            .then(res => res.json())
            .then(data => setAgentTypes(data.types || []))
            .catch(err => console.error("Failed to fetch agent types", err));
    }, []);

    const toggleExpand = (capId: string) => {
        setExpandedCaps(prev => {
            const next = new Set(prev);
            if (next.has(capId)) next.delete(capId);
            else next.add(capId);
            return next;
        });
    };

    const toggleGroupTools = (cap: any) => {
        const allGroupEnabled = cap.tools.every((t: string) => draftAgent.tools.includes(t));
        if (draftAgent.tools.includes("all")) {
            // Switch from "all" to explicit list minus this group
            const allToolsFlat = availableCapabilities.flatMap((c: any) => c.tools);
            if (allGroupEnabled) {
                const newTools = allToolsFlat.filter((t: string) => !cap.tools.includes(t));
                setDraftAgent({ ...draftAgent, tools: newTools });
            } else {
                setDraftAgent({ ...draftAgent, tools: [...draftAgent.tools, ...cap.tools] });
            }
        } else {
            if (allGroupEnabled) {
                const newTools = draftAgent.tools.filter((t: string) => !cap.tools.includes(t));
                setDraftAgent({ ...draftAgent, tools: newTools });
            } else {
                const newTools = [...draftAgent.tools, ...cap.tools.filter((t: string) => !draftAgent.tools.includes(t))];
                setDraftAgent({ ...draftAgent, tools: newTools });
            }
        }
    };

    const toggleSingleTool = (toolName: string, cap: any) => {
        if (draftAgent.tools.includes("all")) {
            // Switch from "all" to explicit list minus this tool
            const allToolsFlat = availableCapabilities.flatMap((c: any) => c.tools);
            const newTools = allToolsFlat.filter((t: string) => t !== toolName);
            setDraftAgent({ ...draftAgent, tools: newTools });
        } else {
            if (draftAgent.tools.includes(toolName)) {
                const newTools = draftAgent.tools.filter((t: string) => t !== toolName);
                setDraftAgent({ ...draftAgent, tools: newTools });
            } else {
                setDraftAgent({ ...draftAgent, tools: [...draftAgent.tools, toolName] });
            }
        }
    };

    const generatePrompt = async () => {
        if (!promptDescription.trim()) return;
        setIsGenerating(true);
        try {
            // Collect selected tool names with descriptions
            const agentType = draftAgent.type || 'conversational';
            const autoToolNames = [
                ...(AUTO_TOOLS_BY_TYPE.all_types || []),
                ...(AUTO_TOOLS_BY_TYPE[agentType] || []),
            ];
            const selectedTools: string[] = [];
            for (const cap of availableCapabilities) {
                for (const tool of (cap.toolDetails || cap.tools.map((t: string) => ({ name: t, description: '' })))) {
                    if (
                        autoToolNames.includes(tool.name) ||
                        draftAgent.tools.includes('all') ||
                        draftAgent.tools.includes(tool.name)
                    ) {
                        selectedTools.push(tool.description ? `${tool.name} - ${tool.description}` : tool.name);
                    }
                }
            }

            const delegateAgents = agentType === 'delegate'
                ? agents
                    .filter((a: any) => a.id !== draftAgent.id && a.type !== 'builder')
                    .map((a: any) => ({ id: a.id, name: a.name, description: a.description || '', type: a.type }))
                : [];

            const res = await fetch('/api/agents/generate-prompt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    description: promptDescription,
                    agent_type: agentType,
                    tools: selectedTools,
                    existing_prompt: draftAgent.system_prompt || '',
                    agents: delegateAgents,
                }),
                signal: AbortSignal.timeout(180_000), // 3 minutes timeout for LLM generation
            });
            if (!res.ok) throw new Error('Failed to generate prompt');
            const data = await res.json();
            setDraftAgent({ ...draftAgent, system_prompt: data.system_prompt });
            setPromptDescription('');
        } catch (err) {
            console.error('Failed to generate prompt:', err);
        } finally {
            setIsGenerating(false);
        }
    };

    return (
        <div className="grid grid-cols-1 md:grid-cols-12 gap-10">
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
            {/* List */}
            <div className="md:col-span-4 border-r border-zinc-800 pr-4 flex flex-col max-h-[calc(100vh-180px)] sticky top-0 self-start">
                <div className="mb-4 flex justify-between items-center">
                    <h3 className="text-sm font-bold text-zinc-400">YOUR AGENTS</h3>
                    <button
                        onClick={() => {
                            const newAgent = {
                                id: `agent_${Date.now()}`,
                                name: "New Agent",
                                description: "A custom agent.",
                                system_prompt: "You are a helpful assistant.",
                                tools: [],
                                repos: [],
                                type: "conversational",
                                avatar: "default",
                                max_turns: 30,
                            };
                            setDraftAgent(newAgent);
                            setSelectedAgentId(newAgent.id);
                        }}
                        className="p-1.5 hover:bg-zinc-800 text-white transition-colors border border-dashed border-zinc-600 hover:border-white"
                        title="Create New Agent"
                    >
                        <Plus className="h-4 w-4" />
                    </button>
                </div>

                <div className="space-y-2 flex-1 overflow-y-auto modern-scrollbar">
                    {loadingAgents && agents.length === 0 && (
                        <div className="flex items-center gap-2 text-zinc-500 text-sm py-4">
                            <Loader2 className="w-4 h-4 animate-spin" />
                            Loading agents…
                        </div>
                    )}
                    {Array.isArray(agents) && agents.map((a: any) => (
                        <div
                            key={a.id}
                            onClick={() => {
                                setSelectedAgentId(a.id);
                                setDraftAgent({ ...a }); // Deep copy to draft
                            }}
                            className={`p-3 border cursor-pointer transition-all group relative
                            ${selectedAgentId === a.id
                                    ? 'bg-zinc-900 border-white shadow-lg'
                                    : 'bg-black border-zinc-800 hover:border-zinc-600'
                                }`}
                        >
                            <div className="flex items-center gap-3">
                                <div className={`h-8 w-8 rounded-full flex items-center justify-center text-xs font-bold
                                ${selectedAgentId === a.id ? 'bg-white text-black' : 'bg-zinc-800 text-zinc-400'}
                            `}>
                                    {a.name.substring(0, 2).toUpperCase()}
                                </div>
                                <div className="flex-1 min-w-0">
                                    <div className="text-xs font-bold text-white truncate">{a.name}</div>
                                    <div className="text-[10px] text-zinc-500 truncate">{a.description}</div>
                                </div>
                            </div>
                            <button
                                onClick={(e) => {
                                    e.stopPropagation();
                                    onDeleteAgent(a.id);
                                }}
                                className="absolute top-2 right-2 p-1 text-zinc-600 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                            >
                                <Trash className="h-3 w-3" />
                            </button>
                        </div>
                    ))}
                </div>
            </div>

            {/* Edit Form */}
            <div className="md:col-span-8 pl-4">
                {draftAgent ? (
                    <div className="space-y-6 h-full flex flex-col pb-4">
                        {/* ── Orchestration agents are read-only here ─────────── */}
                        {draftAgent.type === 'orchestrator' ? (
                            <div className="flex flex-col items-center justify-center h-full min-h-[400px] gap-6 text-center">
                                <div className="relative">
                                    <div className="h-20 w-20 rounded-full bg-gradient-to-br from-purple-900/60 to-violet-900/40 border border-purple-700/50 flex items-center justify-center">
                                        <svg className="h-9 w-9 text-purple-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                                            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
                                        </svg>
                                    </div>
                                    <div className="absolute -top-1 -right-1 h-5 w-5 rounded-full bg-purple-600 flex items-center justify-center">
                                        <Lock className="h-2.5 w-2.5 text-white" />
                                    </div>
                                </div>
                                <div className="space-y-2">
                                    <h4 className="text-sm font-bold text-white">{draftAgent.name}</h4>
                                    <p className="text-[11px] text-zinc-500 max-w-[280px] leading-relaxed">
                                        This is an <span className="text-purple-400 font-semibold">Orchestration Agent</span>. Its workflow, steps, and configuration are managed in the dedicated Orchestrations editor.
                                    </p>
                                </div>
                                <div className="px-5 py-3 border border-dashed border-purple-800/60 bg-purple-950/20 rounded text-[10px] text-purple-300 flex items-center gap-2">
                                    <ExternalLink className="h-3 w-3 flex-shrink-0" />
                                    Open the <strong>Orchestrations</strong> menu to edit this agent's workflow
                                </div>
                            </div>
                        ) : (<>
                            <div className="flex items-center justify-between">
                                <h3 className="text-sm font-bold text-white flex items-center gap-2">
                                    <div className="h-2 w-2 rounded-full bg-purple-500" />
                                    {agents.some((a: any) => a.id === draftAgent.id) ? `EDITING: ${draftAgent.name.toUpperCase()}` : 'NEW AGENT'}
                                </h3>
                                {agentSubTab === 'config' && (
                                    <button
                                        onClick={handleSaveAgent}
                                        disabled={isSaving}
                                        className="flex items-center gap-2 px-4 py-1.5 bg-white text-black text-xs font-bold hover:bg-zinc-200 disabled:opacity-60 disabled:cursor-not-allowed"
                                    >
                                        {isSaving
                                            ? <><Loader2 className="h-3 w-3 animate-spin" /> SAVING…</>
                                            : <><Save className="h-3 w-3" /> SAVE AGENT</>}
                                    </button>
                                )}
                            </div>

                            {/* Sub-tab row */}
                            <div className="flex gap-0 border-b border-zinc-800">
                                {[{ id: 'config', label: 'Configuration' }, { id: 'messaging', label: 'Messaging Channels' }].map(t => (
                                    <button
                                        key={t.id}
                                        onClick={() => {
                                            setAgentSubTab(t.id as any);
                                            if (t.id === 'messaging' && draftAgent.id) {
                                                fetch(`/api/messaging/channels?agent_id=${draftAgent.id}`)
                                                    .then(r => r.ok ? r.json() : [])
                                                    .then(d => setAgentChannels(Array.isArray(d) ? d : []))
                                                    .catch(() => setAgentChannels([]));
                                            }
                                        }}
                                        className={`px-4 py-2 text-xs font-bold transition-all border-b-2 -mb-px
                                    ${agentSubTab === t.id ? 'text-white border-white' : 'text-zinc-500 border-transparent hover:text-zinc-300'}`}
                                    >
                                        {t.label}
                                    </button>
                                ))}
                            </div>

                            {/* ── Configuration sub-tab ──────────────────────── */}
                            {agentSubTab === 'config' && (
                                <div className="space-y-6 flex-1 flex flex-col min-h-0">
                                    <div className="grid grid-cols-2 gap-6">
                                        <div className="space-y-1">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Name</label>
                                            <input
                                                type="text"
                                                value={draftAgent.name}
                                                onChange={e => setDraftAgent({ ...draftAgent, name: e.target.value })}
                                                className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none"
                                            />
                                        </div>
                                        <div className="space-y-1">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Description</label>
                                            <input
                                                type="text"
                                                value={draftAgent.description}
                                                onChange={e => setDraftAgent({ ...draftAgent, description: e.target.value })}
                                                className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none"
                                            />
                                        </div>
                                        <div className="space-y-1">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Agent Type</label>
                                            <select
                                                value={draftAgent.type || 'conversational'}
                                                onChange={e => {
                                                    const newType = e.target.value;
                                                    const oldAutoTools = AUTO_TOOLS_BY_TYPE[draftAgent.type] || [];
                                                    const cleanedTools = draftAgent.tools.filter(
                                                        (t: string) => !oldAutoTools.includes(t)
                                                    );
                                                    const defaultMaxTurns = newType === 'code' ? 50 : 30;
                                                    setDraftAgent({
                                                        ...draftAgent,
                                                        type: newType,
                                                        tools: cleanedTools,
                                                        max_turns: draftAgent.max_turns ?? defaultMaxTurns,
                                                    });
                                                }}
                                                className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none"
                                            >
                                                {agentTypes.map(t => (
                                                    <option key={t.value} value={t.value}>{t.label}</option>
                                                ))}
                                            </select>
                                            <p className="text-[9px] text-zinc-500 mt-1">
                                                {agentTypes.find(t => t.value === (draftAgent.type || 'conversational'))?.description}
                                            </p>
                                        </div>

                                        {/* Model Selection */}
                                        <div className="space-y-1">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Model</label>
                                            <select
                                                value={draftAgent.model || ''}
                                                onChange={e => setDraftAgent({ ...draftAgent, model: e.target.value || null })}
                                                className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none"
                                            >
                                                <option value="">Use Default ({defaultModel || 'not set'})</option>
                                                {providers && Object.entries(providers).map(([providerKey, info]) => {
                                                    if (!info.available || info.models.length === 0) return null;
                                                    const providerLabel = providerKey.charAt(0).toUpperCase() + providerKey.slice(1);
                                                    return (
                                                        <optgroup key={providerKey} label={providerLabel}>
                                                            {info.models.map((m: string) => (
                                                                <option key={m} value={m}>{m}</option>
                                                            ))}
                                                        </optgroup>
                                                    );
                                                })}
                                            </select>
                                            <p className="text-[9px] text-zinc-500 mt-1">Override the default model for this agent. Leave empty to use the system default.</p>
                                        </div>

                                        {/* Max Turns */}
                                        <div className="space-y-1">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Max Turns</label>
                                            <input
                                                type="number"
                                                min={1}
                                                max={200}
                                                value={draftAgent.max_turns ?? (draftAgent.type === 'code' ? 50 : 30)}
                                                onChange={e => setDraftAgent({ ...draftAgent, max_turns: parseInt(e.target.value) || 30 })}
                                                className="w-full bg-zinc-950 border border-zinc-800 p-3 text-xs text-white focus:border-white focus:outline-none"
                                            />
                                            <p className="text-[9px] text-zinc-500 mt-1">Max reasoning turns per request. Orchestration steps override this value.</p>
                                        </div>
                                    </div>

                                    {draftAgent.type === 'code' && (
                                        <div className="space-y-1">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Linked Repositories</label>
                                            <div className="bg-zinc-950 border border-zinc-800 p-3 flex flex-wrap gap-2 min-h-[50px]">
                                                {repos.length === 0 && <span className="text-xs text-zinc-500">No repositories indexed yet.</span>}
                                                {repos.map(repo => {
                                                    const isLinked = draftAgent.repos?.includes(repo.id);
                                                    return (
                                                        <button
                                                            key={repo.id}
                                                            onClick={() => {
                                                                const currentRepos = draftAgent.repos || [];
                                                                if (isLinked) {
                                                                    setDraftAgent({ ...draftAgent, repos: currentRepos.filter((id: string) => id !== repo.id) });
                                                                } else {
                                                                    setDraftAgent({ ...draftAgent, repos: [...currentRepos, repo.id] });
                                                                }
                                                            }}
                                                            className={`px-3 py-1.5 text-xs font-bold border transition-colors ${isLinked
                                                                ? 'bg-white text-black border-white'
                                                                : 'bg-zinc-900 border-zinc-800 text-zinc-400 hover:border-zinc-500'
                                                                }`}
                                                        >
                                                            {repo.name} {isLinked && '✓'}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                            <p className="text-[9px] text-zinc-500 mt-1">Select indexed repositories for semantic code search access.</p>
                                        </div>
                                    )}

                                    {draftAgent.type === 'code' && (
                                        <div className="space-y-1">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Linked Databases</label>
                                            <div className="bg-zinc-950 border border-zinc-800 p-3 flex flex-wrap gap-2 min-h-[50px]">
                                                {dbConfigs.length === 0 && <span className="text-xs text-zinc-500">No databases configured yet.</span>}
                                                {dbConfigs.map((db: any) => {
                                                    const isLinked = draftAgent.db_configs?.includes(db.id);
                                                    return (
                                                        <button
                                                            key={db.id}
                                                            onClick={() => {
                                                                const currentDbs = draftAgent.db_configs || [];
                                                                if (isLinked) {
                                                                    setDraftAgent({ ...draftAgent, db_configs: currentDbs.filter((id: string) => id !== db.id) });
                                                                } else {
                                                                    setDraftAgent({ ...draftAgent, db_configs: [...currentDbs, db.id] });
                                                                }
                                                            }}
                                                            className={`px-3 py-1.5 text-xs font-bold border transition-colors ${isLinked
                                                                ? 'bg-white text-black border-white'
                                                                : 'bg-zinc-900 border-zinc-800 text-zinc-400 hover:border-zinc-500'
                                                                }`}
                                                        >
                                                            {db.name} <span className="opacity-50">{db.db_type}</span> {isLinked && '✓'}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                            <p className="text-[9px] text-zinc-500 mt-1">Select databases to inject schema context into the agent's system prompt.</p>
                                        </div>
                                    )}

                                    {draftAgent.type === 'delegate' ? (
                                        /* ── Delegate Agent: Sub-Agent Selector ── */
                                        <div className="space-y-3">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">Sub-Agents (Delegation Targets)</label>
                                            <p className="text-[9px] text-zinc-500 -mt-1">
                                                Select which agents this delegate can route tasks to. Leave all unchecked to allow delegation to any agent.
                                            </p>
                                            {(() => {
                                                const otherAgents = agents.filter((a: any) =>
                                                    a.id !== draftAgent.id && a.type !== 'builder'
                                                );
                                                const selectedIds: string[] = draftAgent.delegate_agent_ids || [];
                                                const allSelected = selectedIds.length === 0;
                                                if (otherAgents.length === 0) {
                                                    return (
                                                        <div className="p-6 border border-dashed border-zinc-800 text-center text-zinc-600">
                                                            <Bot className="h-6 w-6 mx-auto opacity-20 mb-2" />
                                                            <p className="text-xs">No other agents available. Create agents first, then assign them here.</p>
                                                        </div>
                                                    );
                                                }
                                                return (
                                                    <div className="space-y-2">
                                                        {/* All agents toggle */}
                                                        <div
                                                            onClick={() => setDraftAgent({ ...draftAgent, delegate_agent_ids: [] })}
                                                            className={`p-3 border cursor-pointer transition-all flex items-center gap-3
                                                                ${allSelected ? 'bg-zinc-900 border-zinc-600' : 'bg-black border-zinc-800 hover:border-zinc-600'}`}
                                                        >
                                                            <div className={`w-3 h-3 border flex-shrink-0 flex items-center justify-center
                                                                ${allSelected ? 'bg-green-500 border-green-500' : 'border-zinc-600'}`}
                                                            />
                                                            <div className="flex-1 min-w-0">
                                                                <div className="text-xs font-bold text-white">All Agents</div>
                                                                <div className="text-[9px] text-zinc-500">Allow delegation to any available agent</div>
                                                            </div>
                                                            {allSelected && <span className="text-[9px] px-1.5 py-0.5 bg-green-900/50 text-green-400 border border-green-900 rounded">ACTIVE</span>}
                                                        </div>

                                                        {/* Individual agents */}
                                                        <div className="grid grid-cols-2 gap-2">
                                                            {otherAgents.map((a: any) => {
                                                                const isSelected = selectedIds.includes(a.id);
                                                                return (
                                                                    <div
                                                                        key={a.id}
                                                                        onClick={() => {
                                                                            let newIds: string[];
                                                                            if (isSelected) {
                                                                                newIds = selectedIds.filter((id: string) => id !== a.id);
                                                                            } else {
                                                                                newIds = [...selectedIds, a.id];
                                                                            }
                                                                            setDraftAgent({ ...draftAgent, delegate_agent_ids: newIds });
                                                                        }}
                                                                        className={`p-3 border cursor-pointer transition-all
                                                                            ${isSelected ? 'bg-zinc-900 border-zinc-600' : allSelected ? 'bg-zinc-900/30 border-zinc-800 opacity-60' : 'bg-black border-zinc-800 hover:border-zinc-600'}`}
                                                                    >
                                                                        <div className="flex items-center gap-2">
                                                                            <div className={`w-3 h-3 border flex-shrink-0
                                                                                ${isSelected ? 'bg-green-500 border-green-500' : 'border-zinc-600'}`}
                                                                            />
                                                                            <div className="flex-1 min-w-0">
                                                                                <div className="text-xs font-bold text-white truncate">{a.name}</div>
                                                                                <div className="text-[9px] text-zinc-500 truncate">{a.description}</div>
                                                                            </div>
                                                                            <span className="text-[9px] px-1 bg-zinc-800 text-zinc-500 rounded capitalize flex-shrink-0">{a.type}</span>
                                                                        </div>
                                                                    </div>
                                                                );
                                                            })}
                                                        </div>
                                                    </div>
                                                );
                                            })()}
                                        </div>
                                    ) : (
                                    <div className="space-y-3">
                                        <label className="text-[10px] font-bold text-zinc-500 uppercase">Capabilities (Tools)</label>
                                        {loadingCapabilities ? (
                                            /* ── Skeleton loader ── */
                                            <div className="grid grid-cols-2 gap-4">
                                                {Array.from({ length: 8 }).map((_, i) => (
                                                    <div key={i} className="border border-zinc-800 bg-black p-4 space-y-2 animate-pulse">
                                                        <div className="flex items-center gap-2">
                                                            <div className="w-3 h-3 rounded-sm bg-zinc-800" />
                                                            <div className="h-2.5 bg-zinc-800 rounded w-24" />
                                                        </div>
                                                        <div className="h-2 bg-zinc-800/70 rounded w-32 ml-5" />
                                                    </div>
                                                ))}
                                            </div>
                                        ) : (() => {
                                            const agentType = draftAgent.type || 'conversational';
                                            const autoTools = new Set([
                                                ...(AUTO_TOOLS_BY_TYPE.all_types || []),
                                                ...(AUTO_TOOLS_BY_TYPE[agentType] || []),
                                            ]);
                                            return (
                                                <div className="grid grid-cols-2 gap-4">
                                                    {availableCapabilities.map((cap: any) => {
                                                        const toolDetails: { name: string, description: string }[] = cap.toolDetails || cap.tools.map((t: string) => ({ name: t, description: '' }));
                                                        const hasMultipleTools = toolDetails.length > 1;
                                                        const isExpanded = expandedCaps.has(cap.id);
                                                        const isAutoGroup = cap.tools.every((t: string) => autoTools.has(t));
                                                        const enabledCount = cap.tools.filter((t: string) =>
                                                            isAutoGroup || draftAgent.tools.includes("all") || draftAgent.tools.includes(t)
                                                        ).length;
                                                        const allGroupEnabled = enabledCount === cap.tools.length;
                                                        const someEnabled = enabledCount > 0 && !allGroupEnabled;

                                                        return (
                                                            <div
                                                                key={cap.id}
                                                                className={`border transition-colors
                                                            ${isAutoGroup
                                                                        ? 'bg-zinc-900/60 border-blue-900/40'
                                                                        : allGroupEnabled
                                                                            ? 'bg-zinc-900 border-zinc-600'
                                                                            : someEnabled
                                                                                ? 'bg-zinc-900/50 border-zinc-700'
                                                                                : 'bg-black border-zinc-800 opacity-50'
                                                                    }`}
                                                            >
                                                                <div className={`p-4 flex items-center gap-2 transition-colors ${isAutoGroup ? 'cursor-default' : 'cursor-pointer hover:bg-zinc-800/30'}`}
                                                                    onClick={() => {
                                                                        if (isAutoGroup) return;
                                                                        if (hasMultipleTools) {
                                                                            toggleExpand(cap.id);
                                                                        } else {
                                                                            toggleGroupTools(cap);
                                                                        }
                                                                    }}
                                                                >
                                                                    {isAutoGroup ? (
                                                                        <Lock className="w-3 h-3 text-blue-400 flex-shrink-0" />
                                                                    ) : (
                                                                        <div
                                                                            onClick={(e) => {
                                                                                if (hasMultipleTools) {
                                                                                    e.stopPropagation();
                                                                                    toggleGroupTools(cap);
                                                                                }
                                                                            }}
                                                                            className={`w-3 h-3 border flex-shrink-0 flex items-center justify-center cursor-pointer
                                                                        ${allGroupEnabled
                                                                                    ? 'bg-green-500 border-green-500'
                                                                                    : someEnabled
                                                                                        ? 'bg-yellow-500 border-yellow-500'
                                                                                        : 'border-zinc-600'
                                                                                }`}
                                                                        >
                                                                            {someEnabled && <div className="w-1.5 h-0.5 bg-white"></div>}
                                                                        </div>
                                                                    )}
                                                                    <span className="text-xs font-bold text-white truncate flex-1">{cap.label}</span>
                                                                    {isAutoGroup && <span className="text-[9px] px-1.5 py-0.5 bg-blue-900/50 text-blue-400 border border-blue-900 rounded">DEFAULT</span>}
                                                                    {!isAutoGroup && cap.toolType === 'custom' && <span className="text-[9px] px-1 bg-zinc-800 text-zinc-400 rounded">CUSTOM</span>}
                                                                    {!isAutoGroup && cap.toolType === 'mcp' && <span className="text-[9px] px-1 bg-blue-900/50 text-blue-400 border border-blue-900 rounded">MCP</span>}
                                                                    {!isAutoGroup && hasMultipleTools && (
                                                                        <span className="text-[9px] text-zinc-500">{enabledCount}/{cap.tools.length}</span>
                                                                    )}
                                                                    {!isAutoGroup && hasMultipleTools && (
                                                                        isExpanded
                                                                            ? <ChevronDown className="h-3 w-3 text-zinc-500 flex-shrink-0" />
                                                                            : <ChevronRight className="h-3 w-3 text-zinc-500 flex-shrink-0" />
                                                                    )}
                                                                </div>

                                                                {!isExpanded && (
                                                                    <div className="px-4 pb-3 -mt-1">
                                                                        <p className="text-[9px] text-zinc-500 pl-5 line-clamp-2">
                                                                            {isAutoGroup ? `Included by default for ${agentType} agents` : cap.description}
                                                                        </p>
                                                                    </div>
                                                                )}

                                                                {isExpanded && hasMultipleTools && !isAutoGroup && (
                                                                    <div className="border-t border-zinc-800 px-3 py-2 space-y-1 max-h-[200px] overflow-y-auto">
                                                                        {toolDetails.map((tool: { name: string, description: string }) => {
                                                                            const isToolAuto = autoTools.has(tool.name);
                                                                            const isToolEnabled = isToolAuto || draftAgent.tools.includes("all") || draftAgent.tools.includes(tool.name);
                                                                            return (
                                                                                <div
                                                                                    key={tool.name}
                                                                                    onClick={() => !isToolAuto && toggleSingleTool(tool.name, cap)}
                                                                                    className={`flex gap-2.5 py-1.5 px-2 rounded transition-colors ${isToolAuto ? 'cursor-default opacity-60' : 'cursor-pointer hover:bg-zinc-800/40'}`}
                                                                                >
                                                                                    {isToolAuto ? (
                                                                                        <Lock className="w-2.5 h-2.5 text-blue-400 flex-shrink-0 mt-[3px]" />
                                                                                    ) : (
                                                                                        <div className={`w-2.5 h-2.5 border flex-shrink-0 mt-[3px]
                                                                                    ${isToolEnabled
                                                                                                ? 'bg-green-500 border-green-500'
                                                                                                : 'border-zinc-600'
                                                                                            }`}
                                                                                        ></div>
                                                                                    )}
                                                                                    <div className="min-w-0 flex-1">
                                                                                        <div className="text-[10px] font-mono text-zinc-300">{tool.name}</div>
                                                                                        {tool.description && (
                                                                                            <p className="text-[9px] text-zinc-600 mt-0.5 leading-tight line-clamp-2">{tool.description}</p>
                                                                                        )}
                                                                                    </div>
                                                                                </div>
                                                                            );
                                                                        })}
                                                                    </div>
                                                                )}
                                                            </div>
                                                        );
                                                    })}
                                                </div>
                                            );
                                        })()}
                                    </div>
                                    )}

                                    {/* Prompt Generator */}
                                    <div className="space-y-2">
                                        <label className="text-[10px] font-bold text-zinc-500 uppercase flex items-center gap-1.5">
                                            <Sparkles className="h-3 w-3" /> AI Prompt Writer
                                        </label>
                                        <div className="flex gap-2">
                                            <input
                                                type="text"
                                                value={promptDescription}
                                                onChange={e => setPromptDescription(e.target.value)}
                                                onKeyDown={e => e.key === 'Enter' && !isGenerating && generatePrompt()}
                                                placeholder="Describe what this agent should do... e.g. 'A customer support agent for a SaaS product'"
                                                className="flex-1 bg-zinc-950 border border-zinc-800 px-3 py-2 text-xs text-white focus:border-purple-500 focus:outline-none placeholder:text-zinc-600"
                                            />
                                            <button
                                                onClick={generatePrompt}
                                                disabled={isGenerating || !promptDescription.trim()}
                                                className="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:bg-zinc-800 disabled:text-zinc-600 text-white text-xs font-bold flex items-center gap-2 transition-colors"
                                            >
                                                {isGenerating ? (
                                                    <><Loader2 className="h-3 w-3 animate-spin" /> GENERATING...</>
                                                ) : (
                                                    <><Sparkles className="h-3 w-3" /> GENERATE</>
                                                )}
                                            </button>
                                        </div>
                                        <p className="text-[9px] text-zinc-600">Describe the agent&apos;s purpose and the AI will generate a comprehensive system prompt. Tools and date/time context are auto-injected at runtime.</p>
                                    </div>

                                    {/* System Prompt with Preview */}
                                    <div className="space-y-1 flex-1 flex flex-col min-h-0">
                                        <div className="flex items-center justify-between">
                                            <label className="text-[10px] font-bold text-zinc-500 uppercase">System Prompt (The Brain)</label>
                                            <button
                                                onClick={() => setShowPreview(!showPreview)}
                                                className="flex items-center gap-1.5 text-[10px] font-bold text-zinc-500 hover:text-white transition-colors px-2 py-1"
                                            >
                                                {showPreview ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                                                {showPreview ? 'EDIT' : 'PREVIEW'}
                                            </button>
                                        </div>
                                        {showPreview ? (
                                            <div className="w-full flex-1 min-h-[200px] max-h-[500px] overflow-y-auto bg-zinc-950 border border-zinc-800 p-4 text-sm text-zinc-300 leading-relaxed">
                                                {renderTextContent(draftAgent.system_prompt || '*No system prompt yet.*')}
                                            </div>
                                        ) : (
                                            <VaultTextarea
                                                value={draftAgent.system_prompt}
                                                onChange={e => setDraftAgent({ ...draftAgent, system_prompt: e.target.value })}
                                                className="w-full flex-1 min-h-[200px] bg-zinc-950 border border-zinc-800 p-3 text-xs font-mono text-zinc-300 focus:border-white focus:outline-none resize-none leading-relaxed"
                                                placeholder="You are a helpful assistant. Type @ to reference a vault file..."
                                            />
                                        )}
                                    </div>
                                </div>
                            )} {/* end agentSubTab === 'config' */}

                            {/* ── Messaging Channels sub-tab ─────────────────── */}
                            {agentSubTab === 'messaging' && (
                                <div className="space-y-4">
                                    <p className="text-[10px] text-zinc-500">
                                        Messaging channels bound to this agent. Configure them in full from <strong className="text-zinc-300">Settings → Messaging</strong>.
                                    </p>
                                    {agentChannels.length === 0 ? (
                                        <div className="p-8 border border-dashed border-zinc-800 text-center text-zinc-600 space-y-3">
                                            <MessageSquare className="h-8 w-8 mx-auto opacity-20" />
                                            <p className="text-xs">No messaging channels bound to this agent yet.</p>
                                            <p className="text-[10px]">Go to <strong className="text-zinc-400">Settings → Messaging</strong> and select this agent when creating a channel.</p>
                                        </div>
                                    ) : (
                                        <div className="space-y-2">
                                            {agentChannels.map((ch: any) => {
                                                const EMOJI: Record<string, string> = { telegram: '✈️', discord: '🎮', slack: '💬', teams: '📘', whatsapp: '📱' };
                                                return (
                                                    <div key={ch.id} className="flex items-center gap-3 p-3 border border-zinc-800 bg-zinc-950">
                                                        <span className="text-lg">{EMOJI[ch.platform] ?? '🤖'}</span>
                                                        <div className="flex-1 min-w-0">
                                                            <div className="text-xs font-bold text-white">{ch.name}</div>
                                                            <div className="text-[10px] text-zinc-500 capitalize">{ch.platform}{ch.multi_agent_mode ? ' · multi-agent' : ''}</div>
                                                        </div>
                                                        {ch.status === 'running'
                                                            ? <span className="flex items-center gap-1 text-[10px] text-green-400"><CheckCircle className="h-3 w-3" /> Running</span>
                                                            : ch.status === 'error'
                                                                ? <span className="flex items-center gap-1 text-[10px] text-red-400"><XCircle className="h-3 w-3" /> Error</span>
                                                                : <span className="flex items-center gap-1 text-[10px] text-zinc-500"><Square className="h-3 w-3" /> Stopped</span>
                                                        }
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            )}
                        </>)}
                    </div>
                ) : (
                    <div className="h-full flex flex-col items-center justify-center text-zinc-600 space-y-4">
                        <Bot className="h-12 w-12 opacity-20" />
                        <p className="text-sm">Select an agent to edit or create a new one.</p>
                    </div>
                )}
            </div>
        </div>
    );
};
