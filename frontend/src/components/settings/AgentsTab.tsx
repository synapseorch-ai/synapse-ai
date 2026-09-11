/* eslint-disable @typescript-eslint/no-explicit-any */
import { Bot, Plus, Save, Trash, Copy, ChevronDown, ChevronRight, Lock, Sparkles, Eye, EyeOff, MessageSquare, ExternalLink, CheckCircle, XCircle, Square } from 'lucide-react';
import { Combobox, Label, SearchInput, Select, Spinner } from '@/components/ui';
import { matchesQuery } from '@/lib/search';
import { humanizeToolName } from '@/lib/toolNames';
import { VaultTextarea } from '@/components/VaultMention';
import { CAPABILITIES, AUTO_TOOLS_BY_TYPE } from './types';
import { renderTextContent } from '@/lib/utils';
import { useDispatch } from 'react-redux';
import { AppDispatch } from '@/store';
import { addAgent, updateAgent } from '@/store/settingsSlice';
import { ToastNotification } from './ToastNotification';
import { AgentActivity, ActivityTab } from './AgentActivity';
import { ToolPolicyFields, gatingCategories, POLICY_CATEGORIES, type ToolPolicy } from './ToolPolicy';

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
    /** Models with a native reasoning channel, from the models endpoint. The
     *  list is served rather than restated here: a second copy of the
     *  capability table would be wrong the first time a family ships and only
     *  one of them was updated. */
    reasoningCapable?: string[];
    defaultModel?: string;
    loadingAgents?: boolean;
}

import React, { useState, useEffect } from 'react';

export const AgentsTab = ({
    agents, selectedAgentId, setSelectedAgentId,
    draftAgent, setDraftAgent, availableCapabilities, loadingCapabilities = false, customTools,
    onDeleteAgent, providers, reasoningCapable, defaultModel, loadingAgents = false
}: AgentsTabProps) => {
    const dispatch = useDispatch<AppDispatch>();
    const [repos, setRepos] = useState<any[]>([]);
    const [allSkills, setAllSkills] = useState<any[]>([]);
    const [dbConfigs, setDbConfigs] = useState<any[]>([]);
    const [agentTypes, setAgentTypes] = useState<{ value: string; label: string; description: string }[]>([]);
    const [expandedCaps, setExpandedCaps] = useState<Set<string>>(new Set());
    const [promptDescription, setPromptDescription] = useState('');
    const [isGenerating, setIsGenerating] = useState(false);
    const [showPreview, setShowPreview] = useState(false);
    const [agentSubTab, setAgentSubTab] = useState<'config' | 'messaging' | ActivityTab>('config');
    const [agentChannels, setAgentChannels] = useState<any[]>([]);
    // The workspace floor an unset category inherits, and the widgets that
    // publish this agent — both read-only here, both needed to say what a
    // choice will actually do before it is saved.
    const [policyDefaults, setPolicyDefaults] = useState<ToolPolicy>({});
    const [widgets, setWidgets] = useState<any[]>([]);
    const [isSaving, setIsSaving] = useState(false);
    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'error' } | null>(null);
    const [aiBuilderOpen, setAiBuilderOpen] = useState(false);
    const [aiBuilderDesc, setAiBuilderDesc] = useState('');
    const [agentQuery, setAgentQuery] = useState('');
    const [isBuilding, setIsBuilding] = useState(false);

    const showToast = (message: string, type: 'success' | 'error') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), 4000);
    };

    const handleDuplicate = async (agent: any, e: React.MouseEvent) => {
        e.stopPropagation();
        const copy = { ...agent, id: `agent_${Date.now()}`, name: `${agent.name} (Copy)` };
        delete copy.created_at;
        delete copy.updated_at;
        try {
            const res = await fetch('/api/agents', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(copy),
            });
            if (res.ok) {
                const saved = await res.json();
                dispatch(addAgent(saved));
                setSelectedAgentId(saved.id);
                setDraftAgent(saved);
                showToast('Agent duplicated', 'success');
            } else {
                showToast('Failed to duplicate agent', 'error');
            }
        } catch {
            showToast('Error duplicating agent', 'error');
        }
    };

    const buildAgentWithAI = async () => {
        if (!aiBuilderDesc.trim() || isBuilding) return;
        setIsBuilding(true);
        try {
            const available_tools = availableCapabilities.flatMap((cap: any) =>
                (cap.toolDetails || cap.tools.map((t: string) => ({ name: t, description: '' })))
                    .map((t: any) => ({ name: t.name, description: t.description || '' }))
            );
            const available_repos = repos.map((r: any) => ({ id: r.id, name: r.name }));
            const available_db_configs = dbConfigs.map((d: any) => ({ id: d.id, name: d.name, db_type: d.db_type || '' }));

            const res = await fetch('/api/agents/build', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ description: aiBuilderDesc, available_tools, available_repos, available_db_configs }),
                signal: AbortSignal.timeout(60_000),
            });
            if (res.ok) {
                const saved = await res.json();
                dispatch(addAgent(saved));
                setSelectedAgentId(saved.id);
                setDraftAgent(saved);
                setAiBuilderDesc('');
                setAiBuilderOpen(false);
                showToast(`Agent "${saved.name}" created`, 'success');
            } else {
                showToast('Failed to build agent', 'error');
            }
        } catch {
            showToast('Error building agent', 'error');
        } finally {
            setIsBuilding(false);
        }
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
        fetch('/api/skills')
            .then(res => res.json())
            .then(data => setAllSkills(Array.isArray(data) ? data : []))
            .catch(err => console.error("Failed to fetch skills", err));
        fetch('/api/db-configs')
            .then(res => res.json())
            .then(data => setDbConfigs(data))
            .catch(err => console.error("Failed to fetch DB configs", err));
        fetch('/api/agent-types')
            .then(res => res.json())
            .then(data => setAgentTypes(data.types || []))
            .catch(err => console.error("Failed to fetch agent types", err));
        fetch('/api/settings')
            .then(res => res.json())
            .then(data => setPolicyDefaults(data?.tool_policy_defaults || {}))
            .catch(err => console.error("Failed to fetch settings", err));
        fetch('/api/widgets')
            .then(res => res.json())
            .then(data => setWidgets(Array.isArray(data) ? data : []))
            .catch(err => console.error("Failed to fetch widgets", err));
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

    const visibleAgents = Array.isArray(agents)
        ? agents.filter((a: any) => matchesQuery(agentQuery, a.name, a.description, a.id))
        : [];

    return (
        <div className="grid grid-cols-1 md:grid-cols-12 gap-10">
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}
            {/* List */}
            <div className="md:col-span-4 border-r border-border pr-4 flex flex-col max-h-[calc(100vh-180px)] sticky top-0 self-start">
                <div className="mb-4 flex justify-between items-center">
                    <h3 className="text-sm font-bold text-text-muted">YOUR AGENTS</h3>
                    <div className="flex items-center gap-1.5">
                        <button
                            onClick={() => setAiBuilderOpen(v => !v)}
                            className={`p-1.5 transition-colors ${aiBuilderOpen ? 'bg-accent text-accent-fg' : 'bg-accent hover:bg-accent-hover text-accent-fg'}`}
                            title="Build Agent with AI"
                        >
                            <Sparkles className="h-4 w-4" />
                        </button>
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
                            className="p-1.5 hover:bg-surface-2 text-text transition-colors border border-dashed border-text-faint hover:border-border-strong rounded-md"
                            title="Create New Agent"
                        >
                            <Plus className="h-4 w-4" />
                        </button>
                    </div>
                </div>

                {aiBuilderOpen && (
                    <div className="mb-3 p-3 border border-dashed border-accent/40 bg-accent-subtle space-y-2 rounded-md">
                        <p className="text-2xs text-accent font-bold uppercase">Build with AI</p>
                        <textarea
                            value={aiBuilderDesc}
                            onChange={e => setAiBuilderDesc(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); buildAgentWithAI(); } }}
                            placeholder="Describe what this agent should do... e.g. 'A customer support agent that searches our knowledge base'"
                            rows={3}
                            className="w-full bg-bg border border-border p-2 text-xs text-text focus:border-accent focus:outline-none placeholder:text-text-faint resize-none rounded-md"
                        />
                        <button
                            onClick={buildAgentWithAI}
                            disabled={isBuilding || !aiBuilderDesc.trim()}
                            className="w-full py-1.5 bg-accent hover:bg-accent-hover disabled:bg-surface-2 disabled:text-text-faint text-accent-fg text-xs font-bold flex items-center justify-center gap-2 transition-colors"
                        >
                            {isBuilding
                                ? <><Spinner size={12} /> BUILDING…</>
                                : <><Sparkles className="h-3 w-3" /> CREATE AGENT</>}
                        </button>
                    </div>
                )}

                {Array.isArray(agents) && agents.length > 4 && (
                    <SearchInput
                        value={agentQuery}
                        onChange={setAgentQuery}
                        placeholder="Search agents…"
                        className="mb-2 shrink-0"
                    />
                )}

                <div className="space-y-2 flex-1 overflow-y-auto modern-scrollbar">
                    {loadingAgents && agents.length === 0 && (
                        <div className="flex items-center gap-2 text-text-faint text-sm py-4">
                            <Spinner size={16} />
                            Loading agents…
                        </div>
                    )}
                    {Array.isArray(agents) && visibleAgents.length === 0 && agentQuery && (
                        <div className="py-6 text-center text-sm text-text-faint">
                            No agents match “{agentQuery}”.
                        </div>
                    )}
                    {visibleAgents.map((a: any) => (
                        <div
                            key={a.id}
                            onClick={() => {
                                setSelectedAgentId(a.id);
                                setDraftAgent({ ...a }); // Deep copy to draft
                            }}
                            className={`p-3 border cursor-pointer transition-all group relative
                            ${selectedAgentId === a.id
                                    ? 'bg-surface-2 border-accent shadow-lg'
                                    : 'bg-surface border-border hover:border-border-strong'
                                } rounded-md`}
                        >
                            <div className="flex items-center gap-3">
                                <div className={`h-8 w-8 rounded-full flex items-center justify-center text-xs font-bold
                                ${selectedAgentId === a.id ? 'bg-accent text-accent-fg' : 'bg-surface-2 text-text-muted'}
                            `}>
                                    {a.name.substring(0, 2).toUpperCase()}
                                </div>
                                <div className="flex-1 min-w-0">
                                    <div className="text-xs font-bold text-text truncate">{a.name}</div>
                                    <div className="text-2xs text-text-faint truncate">{a.description}</div>
                                </div>
                            </div>
                            <button
                                onClick={(e) => handleDuplicate(a, e)}
                                className="absolute top-2 right-7 p-1 text-text-faint hover:text-accent opacity-0 group-hover:opacity-100 transition-opacity"
                                title="Duplicate agent"
                            >
                                <Copy className="h-3 w-3" />
                            </button>
                            <button
                                onClick={(e) => {
                                    e.stopPropagation();
                                    onDeleteAgent(a.id);
                                }}
                                aria-label={`Delete ${a.name}`}
                                title={`Delete ${a.name}`}
                                className="absolute top-2 right-2 p-1 text-text-faint hover:text-danger opacity-0 group-hover:opacity-100 transition-opacity"
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
                                    <div className="h-20 w-20 rounded-full bg-gradient-to-br from-accent/60 to-accent/40 border border-accent/40 flex items-center justify-center">
                                        <svg className="h-9 w-9 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                                            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
                                        </svg>
                                    </div>
                                    <div className="absolute -top-1 -right-1 h-5 w-5 rounded-full bg-accent flex items-center justify-center">
                                        <Lock className="h-2.5 w-2.5 text-text" />
                                    </div>
                                </div>
                                <div className="space-y-2">
                                    <h4 className="text-sm font-bold text-text">{draftAgent.name}</h4>
                                    <p className="text-2xs text-text-faint max-w-[280px] leading-relaxed">
                                        This is an <span className="text-accent font-semibold">Orchestration Agent</span>. Its workflow, steps, and configuration are managed in the dedicated Orchestrations editor.
                                    </p>
                                </div>
                                <div className="px-5 py-3 border border-dashed border-accent/40 bg-accent-subtle rounded-md text-2xs text-accent flex items-center gap-2">
                                    <ExternalLink className="h-3 w-3 flex-shrink-0" />
                                    Open the <strong>Orchestrations</strong> menu to edit this agent's workflow
                                </div>
                            </div>
                        ) : (<>
                            <div className="flex items-center justify-between">
                                <h3 className="text-sm font-bold text-text flex items-center gap-2">
                                    <div className="h-2 w-2 rounded-full bg-accent" />
                                    {agents.some((a: any) => a.id === draftAgent.id) ? `EDITING: ${draftAgent.name.toUpperCase()}` : 'NEW AGENT'}
                                </h3>
                                {agentSubTab === 'config' && (
                                    <button
                                        onClick={handleSaveAgent}
                                        disabled={isSaving}
                                        className="flex items-center gap-2 px-4 py-1.5 bg-accent text-accent-fg text-xs font-bold hover:bg-accent-hover disabled:opacity-60 disabled:cursor-not-allowed"
                                    >
                                        {isSaving
                                            ? <><Spinner size={12} /> SAVING…</>
                                            : <><Save className="h-3 w-3" /> SAVE AGENT</>}
                                    </button>
                                )}
                            </div>

                            {/* Sub-tab row */}
                            <div className="flex gap-0 border-b border-border">
                                {[
                                    { id: 'config', label: 'Configuration' },
                                    { id: 'messaging', label: 'Messaging Channels' },
                                    { id: 'runs', label: 'Runs' },
                                    { id: 'metrics', label: 'Metrics' },
                                    { id: 'usedby', label: 'Used by' },
                                ].map(t => (
                                    <button
                                        key={t.id}
                                        onClick={() => {
                                            setAgentSubTab(t.id as any);
                                            // "Used by" names the agent's channels too, so both tabs
                                            // want the same fetch.
                                            if ((t.id === 'messaging' || t.id === 'usedby') && draftAgent.id) {
                                                fetch(`/api/messaging/channels?agent_id=${draftAgent.id}`)
                                                    .then(r => r.ok ? r.json() : [])
                                                    .then(d => setAgentChannels(Array.isArray(d) ? d : []))
                                                    .catch(() => setAgentChannels([]));
                                            }
                                        }}
                                        className={`px-4 py-2 text-xs font-bold transition-all border-b-2 -mb-px
                                    ${agentSubTab === t.id ? 'text-text border-border-strong' : 'text-text-faint border-transparent hover:text-text'}`}
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
                                            <Label htmlFor="age-draftagent-name" className="block">Name</Label>
                                            <input id="age-draftagent-name"
                                                type="text"
                                                value={draftAgent.name}
                                                onChange={e => setDraftAgent({ ...draftAgent, name: e.target.value })}
                                                className="w-full bg-bg border border-border p-3 text-xs text-text focus:border-border-strong focus:outline-none rounded-md"
                                            />
                                        </div>
                                        <div className="space-y-1">
                                            <Label htmlFor="age-draftagent-description" className="block">Description</Label>
                                            <input id="age-draftagent-description"
                                                type="text"
                                                value={draftAgent.description}
                                                onChange={e => setDraftAgent({ ...draftAgent, description: e.target.value })}
                                                className="w-full bg-bg border border-border p-3 text-xs text-text focus:border-border-strong focus:outline-none rounded-md"
                                            />
                                        </div>
                                        <div className="space-y-1">
                                            <Label className="block">Agent Type</Label>
                                            <Select
                                                value={draftAgent.type || 'conversational'}
                                                aria-label="Agent type"
                                                options={agentTypes.map(t => ({ value: t.value, label: t.label }))}
                                                onChange={newType => {
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
                                            />
                                            <p className="text-2xs text-text-faint mt-1">
                                                {agentTypes.find(t => t.value === (draftAgent.type || 'conversational'))?.description}
                                            </p>
                                        </div>

                                        {/* Model Selection */}
                                        <div className="space-y-1">
                                            <Label className="block">Model</Label>
                                            <Combobox
                                                value={draftAgent.model || '__default__'}
                                                onChange={v => setDraftAgent({ ...draftAgent, model: v === '__default__' ? null : v })}
                                                aria-label="Model"
                                                searchPlaceholder="Search models…"
                                                options={[
                                                    { value: '__default__', label: `Use default (${defaultModel || 'not set'})` },
                                                    ...Object.entries(providers ?? {}).flatMap(([providerKey, info]: [string, any]) =>
                                                        !info.available || info.models.length === 0
                                                            ? []
                                                            : info.models.map((m: string) => ({
                                                                value: m,
                                                                label: m,
                                                                group: providerKey.charAt(0).toUpperCase() + providerKey.slice(1),
                                                            }))),
                                                ]}
                                            />
                                            <p className="text-2xs text-text-faint mt-1">Override the default model for this agent. Leave empty to use the system default.</p>
                                        </div>

                                        {/* Reasoning effort.

                                            Hidden — not disabled — when the
                                            resolved model has no reasoning
                                            channel. A control that cannot
                                            affect anything is worse than an
                                            absent one: it invites the reading
                                            that reasoning was set and ignored. */}
                                        {(reasoningCapable ?? []).includes(draftAgent.model || defaultModel || '') && (
                                            <div className="space-y-1">
                                                <Label className="block">Reasoning</Label>
                                                <div className="flex gap-1">
                                                    {(['off', 'low', 'medium', 'high'] as const).map(level => {
                                                        const active = (draftAgent.reasoning_effort || 'medium') === level;
                                                        return (
                                                            <button
                                                                key={level}
                                                                type="button"
                                                                onClick={() => setDraftAgent({ ...draftAgent, reasoning_effort: level })}
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
                                                <p className="text-2xs text-text-faint mt-1">How much this model thinks before it answers. Higher costs more and takes longer; thinking tokens are billed as output.</p>
                                            </div>
                                        )}

                                        {/* Max Turns */}
                                        <div className="space-y-1">
                                            <Label htmlFor="age-max-turns" className="block">Max Turns</Label>
                                            <input id="age-max-turns"
                                                type="number"
                                                min={1}
                                                max={200}
                                                value={draftAgent.max_turns ?? (draftAgent.type === 'code' ? 50 : 30)}
                                                onChange={e => setDraftAgent({ ...draftAgent, max_turns: parseInt(e.target.value) || 30 })}
                                                className="w-full bg-bg border border-border p-3 text-xs text-text focus:border-border-strong focus:outline-none rounded-md"
                                            />
                                            <p className="text-2xs text-text-faint mt-1">Max reasoning turns per request. Orchestration steps override this value.</p>
                                        </div>
                                    </div>

                                    {draftAgent.type === 'code' && (
                                        <div className="space-y-1">
                                            <Label className="block">Linked Repositories</Label>
                                            <div className="bg-bg border border-border p-3 flex flex-wrap gap-2 min-h-[50px] rounded-md">
                                                {repos.length === 0 && <span className="text-xs text-text-faint">No repositories indexed yet.</span>}
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
                                                                ? 'bg-accent text-accent-fg border-border-strong'
                                                                : 'bg-surface border-border text-text-muted hover:border-text-faint'
                                                                } rounded-md`}
                                                        >
                                                            {repo.name} {isLinked && '✓'}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                            <p className="text-2xs text-text-faint mt-1">Select indexed repositories for semantic code search access.</p>
                                        </div>
                                    )}

                                    {draftAgent.type === 'code' && (
                                        <div className="space-y-1">
                                            <Label className="block">Linked Databases</Label>
                                            <div className="bg-bg border border-border p-3 flex flex-wrap gap-2 min-h-[50px] rounded-md">
                                                {dbConfigs.length === 0 && <span className="text-xs text-text-faint">No databases configured yet.</span>}
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
                                                                ? 'bg-accent text-accent-fg border-border-strong'
                                                                : 'bg-surface border-border text-text-muted hover:border-text-faint'
                                                                } rounded-md`}
                                                        >
                                                            {db.name} <span className="opacity-50">{db.db_type}</span> {isLinked && '✓'}
                                                        </button>
                                                    );
                                                })}
                                            </div>
                                            <p className="text-2xs text-text-faint mt-1">Select databases to inject schema context into the agent's system prompt.</p>
                                        </div>
                                    )}

                                    {draftAgent.type === 'delegate' ? (
                                        /* ── Delegate Agent: Sub-Agent Selector ── */
                                        <div className="space-y-3">
                                            <Label className="block">Sub-Agents (Delegation Targets)</Label>
                                            <p className="text-2xs text-text-faint -mt-1">
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
                                                        <div className="p-6 border border-dashed border-border text-center text-text-faint">
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
                                                                ${allSelected ? 'bg-surface-2 border-border-strong' : 'bg-surface border-border hover:border-border-strong'} rounded-md`}
                                                        >
                                                            <div className={`w-3 h-3 border flex-shrink-0 flex items-center justify-center
                                                                ${allSelected ? 'bg-success border-success/40' : 'border-text-faint'} rounded-md`}
                                                            />
                                                            <div className="flex-1 min-w-0">
                                                                <div className="text-xs font-bold text-text">All Agents</div>
                                                                <div className="text-2xs text-text-faint">Allow delegation to any available agent</div>
                                                            </div>
                                                            {allSelected && <span className="text-2xs px-1.5 py-0.5 bg-success/50 text-success border border-success/40 rounded-md">ACTIVE</span>}
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
                                                                            ${isSelected ? 'bg-surface-2 border-border-strong' : allSelected ? 'bg-surface/60 border-border opacity-60' : 'bg-surface border-border hover:border-border-strong'} rounded-md`}
                                                                    >
                                                                        <div className="flex items-center gap-2">
                                                                            <div className={`w-3 h-3 border flex-shrink-0
                                                                                ${isSelected ? 'bg-success border-success/40' : 'border-text-faint'} rounded-md`}
                                                                            />
                                                                            <div className="flex-1 min-w-0">
                                                                                <div className="text-xs font-bold text-text truncate">{a.name}</div>
                                                                                <div className="text-2xs text-text-faint truncate">{a.description}</div>
                                                                            </div>
                                                                            <span className="text-2xs px-1 bg-surface-2 text-text-faint rounded-md capitalize flex-shrink-0">{a.type}</span>
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
                                        <Label className="block">Capabilities (Tools)</Label>
                                        {loadingCapabilities ? (
                                            /* ── Skeleton loader ── */
                                            <div className="grid grid-cols-2 gap-4">
                                                {Array.from({ length: 8 }).map((_, i) => (
                                                    <div key={i} className="border border-border bg-surface p-4 space-y-2 animate-pulse rounded-md">
                                                        <div className="flex items-center gap-2">
                                                            <div className="w-3 h-3 rounded-sm bg-surface-2" />
                                                            <div className="h-2.5 bg-surface-2 rounded-md w-24" />
                                                        </div>
                                                        <div className="h-2 bg-surface-2/70 rounded-md w-32 ml-5" />
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
                                                                        ? 'bg-surface/60 border-accent/40'
                                                                        : allGroupEnabled
                                                                            ? 'bg-surface-2 border-border-strong'
                                                                            : someEnabled
                                                                                ? 'bg-surface-2/60 border-border-strong'
                                                                                : 'bg-surface border-border opacity-60'
                                                                    } rounded-md`}
                                                            >
                                                                <div className={`p-4 flex items-center gap-2 transition-colors ${isAutoGroup ? 'cursor-default' : 'cursor-pointer hover:bg-surface-2/30'}`}
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
                                                                        <Lock className="w-3 h-3 text-accent flex-shrink-0" />
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
                                                                                    ? 'bg-success border-success/40'
                                                                                    : someEnabled
                                                                                        ? 'bg-warning border-warning/40'
                                                                                        : 'border-text-faint'
                                                                                } rounded-md`}
                                                                        >
                                                                            {someEnabled && <div className="w-1.5 h-0.5 bg-white"></div>}
                                                                        </div>
                                                                    )}
                                                                    <span className="text-xs font-bold text-text truncate flex-1">{cap.label}</span>
                                                                    {isAutoGroup && <span className="text-2xs px-1.5 py-0.5 bg-accent/50 text-accent border border-accent/40 rounded-md">DEFAULT</span>}
                                                                    {!isAutoGroup && cap.toolType === 'custom' && <span className="text-2xs px-1 bg-surface-2 text-text-muted rounded-md">CUSTOM</span>}
                                                                    {!isAutoGroup && cap.toolType === 'mcp' && <span className="text-2xs px-1 bg-accent/50 text-accent border border-accent/40 rounded-md">MCP</span>}
                                                                    {!isAutoGroup && hasMultipleTools && (
                                                                        <span className="text-2xs text-text-faint">{enabledCount}/{cap.tools.length}</span>
                                                                    )}
                                                                    {!isAutoGroup && hasMultipleTools && (
                                                                        isExpanded
                                                                            ? <ChevronDown className="h-3 w-3 text-text-faint flex-shrink-0" />
                                                                            : <ChevronRight className="h-3 w-3 text-text-faint flex-shrink-0" />
                                                                    )}
                                                                </div>

                                                                {!isExpanded && (
                                                                    <div className="px-4 pb-3 -mt-1">
                                                                        <p className="text-2xs text-text-faint pl-5 line-clamp-2">
                                                                            {isAutoGroup ? `Included by default for ${agentType} agents` : cap.description}
                                                                        </p>
                                                                    </div>
                                                                )}

                                                                {isExpanded && hasMultipleTools && !isAutoGroup && (
                                                                    <div className="border-t border-border px-3 py-2 space-y-1 max-h-[200px] overflow-y-auto">
                                                                        {toolDetails.map((tool: { name: string, description: string }) => {
                                                                            const isToolAuto = autoTools.has(tool.name);
                                                                            const isToolEnabled = isToolAuto || draftAgent.tools.includes("all") || draftAgent.tools.includes(tool.name);
                                                                            return (
                                                                                <div
                                                                                    key={tool.name}
                                                                                    onClick={() => !isToolAuto && toggleSingleTool(tool.name, cap)}
                                                                                    className={`flex gap-2.5 py-1.5 px-2 rounded-md transition-colors ${isToolAuto ? 'cursor-default opacity-60' : 'cursor-pointer hover:bg-surface-2/40'}`}
                                                                                >
                                                                                    {isToolAuto ? (
                                                                                        <Lock className="w-2.5 h-2.5 text-accent flex-shrink-0 mt-[3px]" />
                                                                                    ) : (
                                                                                        <div className={`w-2.5 h-2.5 border flex-shrink-0 mt-[3px]
                                                                                    ${isToolEnabled
                                                                                                ? 'bg-success border-success/40'
                                                                                                : 'border-text-faint'
                                                                                            } rounded-md`}
                                                                                        ></div>
                                                                                    )}
                                                                                    <div className="min-w-0 flex-1">
                                                                                        {/* Humanized name leads; the dispatch key stays beside it
                                                                                            (small, mono) because it is what a step stores and what
                                                                                            a run log prints. */}
                                                                                        <div className="text-2xs text-text">
                                                                                            {humanizeToolName(tool.name)}
                                                                                            <span className="ml-1.5 font-mono text-[10px] text-text-faint" translate="no">{tool.name}</span>
                                                                                        </div>
                                                                                        {tool.description && (
                                                                                            <p className="text-2xs text-text-faint mt-0.5 leading-tight line-clamp-2">{tool.description}</p>
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

                                    {/* Tool policy — what the agent may DO with the tools above.
                                        The list is about which tools it holds; this is about what
                                        a call costs. Enforced in core/tool_policy.py. */}
                                    <div className="space-y-1">
                                        <Label className="block">Tool Policy</Label>
                                        <p className="text-2xs text-text-faint -mt-1 mb-2">
                                            Decided by what a tool does, not by its name — so a destructive call can wait
                                            for a person without taking the tool away from the agent.
                                        </p>
                                        <ToolPolicyFields
                                            value={draftAgent.tool_policy || {}}
                                            onChange={(next) => setDraftAgent({ ...draftAgent, tool_policy: next })}
                                            inheritFrom={policyDefaults}
                                        />
                                        {(() => {
                                            const gating = gatingCategories(draftAgent.tool_policy || {}, policyDefaults);
                                            const bound = widgets.filter((w: any) => w.agent_id === draftAgent.id);
                                            if (gating.length === 0 || bound.length === 0) return null;
                                            const names = bound.map((w: any) => w.name || 'Untitled widget').join(', ');
                                            const asked = gating
                                                .map(id => POLICY_CATEGORIES.find(c => c.id === id)?.clause)
                                                .filter(Boolean)
                                                .join(', or ');
                                            return (
                                                <div className="mt-3 bg-warning/30 border border-warning/50 p-3 rounded-md">
                                                    <p className="text-2xs text-text">
                                                        <span className="font-bold">{bound.length === 1 ? 'A chat widget publishes' : 'Chat widgets publish'} this agent</span>{' '}
                                                        ({names}). A visitor there is not a member of this workspace and cannot
                                                        answer an approval, so a call to {asked} will end their turn with a note
                                                        asking them to get in touch. Saving is fine — but nobody will be asked.
                                                    </p>
                                                </div>
                                            );
                                        })()}
                                    </div>

                                    {/* Skills — roster in the prompt, bodies on demand via load_skill */}
                                    <div className="space-y-1">
                                        <Label className="block">Skills</Label>
                                        <div className="bg-bg border border-border p-3 flex flex-wrap gap-2 min-h-[50px] rounded-md">
                                            {allSkills.filter((s: any) => !s.shadowed).length === 0 && (
                                                <span className="text-xs text-text-faint">No skills yet — create one on the Skills page.</span>
                                            )}
                                            {allSkills.filter((s: any) => !s.shadowed).map((skill: any) => {
                                                const isSelected = draftAgent.skills?.includes(skill.name);
                                                return (
                                                    <button
                                                        key={skill.name}
                                                        title={skill.description}
                                                        onClick={() => {
                                                            const current = draftAgent.skills || [];
                                                            setDraftAgent(isSelected
                                                                ? { ...draftAgent, skills: current.filter((n: string) => n !== skill.name) }
                                                                : { ...draftAgent, skills: [...current, skill.name] });
                                                        }}
                                                        className={`px-3 py-1.5 text-xs font-bold border transition-colors ${isSelected
                                                            ? 'bg-accent text-accent-fg border-border-strong'
                                                            : 'bg-surface border-border text-text-muted hover:border-text-faint'
                                                            } rounded-md`}
                                                    >
                                                        {skill.name} {isSelected && '✓'}
                                                    </button>
                                                );
                                            })}
                                        </div>
                                        <p className="text-2xs text-text-faint mt-1">A selected skill costs one roster line in the prompt; the agent loads its full instructions with load_skill only when needed.</p>
                                    </div>

                                    {/* Planning — the native task list, on unless this agent
                                        has no use for one. Above Memory because it is the
                                        cheaper and more commonly wanted of the two. */}
                                    <div className="space-y-1">
                                        <Label className="block">Planning</Label>
                                        <label className="flex items-start gap-2 bg-bg border border-border p-3 rounded-md cursor-pointer">
                                            <input
                                                type="checkbox"
                                                checked={draftAgent.planning !== false}
                                                onChange={(e) => setDraftAgent({ ...draftAgent, planning: e.target.checked })}
                                                className="mt-0.5 accent-[var(--accent)]"
                                            />
                                            <span className="text-xs text-text-muted">
                                                Let this agent write a plan with <span className="font-mono">update_tasks</span> and
                                                keep it up to date as it works. The list shows live in the run log. Turn it off for
                                                one-shot agents, where announcing three tasks costs a turn and tells nobody anything.
                                            </span>
                                        </label>
                                    </div>

                                    {/* Memory — persistent per-agent memory (index eager, rest on demand) */}
                                    <div className="space-y-1">
                                        <Label className="block">Memory</Label>
                                        <label className="flex items-start gap-2 bg-bg border border-border p-3 rounded-md cursor-pointer">
                                            <input
                                                type="checkbox"
                                                checked={!!draftAgent.memory}
                                                onChange={(e) => setDraftAgent({ ...draftAgent, memory: e.target.checked })}
                                                className="mt-0.5 accent-[var(--accent)]"
                                            />
                                            <span className="text-xs text-text-muted">
                                                Give this agent persistent memory: it can save facts with <span className="font-mono">remember</span>,
                                                search past sessions with <span className="font-mono">search_memory</span>, and its memory index
                                                rides the prompt. Sessions leave summaries automatically.
                                            </span>
                                        </label>
                                    </div>

                                    {/* Prompt Generator */}
                                    <div className="space-y-2">
                                        <Label htmlFor="agn-prompt-description" className="flex items-center gap-1.5">
                                            <Sparkles className="h-3 w-3" aria-hidden /> AI Prompt Writer
                                        </Label>
                                        <div className="flex gap-2">
                                            <input
                                                id="agn-prompt-description"
                                                type="text"
                                                value={promptDescription}
                                                onChange={e => setPromptDescription(e.target.value)}
                                                onKeyDown={e => e.key === 'Enter' && !isGenerating && generatePrompt()}
                                                placeholder="Describe what this agent should do... e.g. 'A customer support agent for a SaaS product'"
                                                className="flex-1 bg-bg border border-border px-3 py-2 text-xs text-text focus:border-accent focus:outline-none placeholder:text-text-faint rounded-md"
                                            />
                                            <button
                                                onClick={generatePrompt}
                                                disabled={isGenerating || !promptDescription.trim()}
                                                className="px-4 py-2 bg-accent hover:bg-accent-hover disabled:bg-surface-2 disabled:text-text-faint text-accent-fg text-xs font-bold flex items-center gap-2 transition-colors"
                                            >
                                                {isGenerating ? (
                                                    <><Spinner size={12} /> GENERATING...</>
                                                ) : (
                                                    <><Sparkles className="h-3 w-3" /> GENERATE</>
                                                )}
                                            </button>
                                        </div>
                                        <p className="text-2xs text-text-faint">Describe the agent&apos;s purpose and the AI will generate a comprehensive system prompt. Tools and date/time context are auto-injected at runtime.</p>
                                    </div>

                                    {/* System Prompt with Preview */}
                                    <div className="space-y-1 flex-1 flex flex-col min-h-0">
                                        <div className="flex items-center justify-between">
                                            <Label className="block">System Prompt (The Brain)</Label>
                                            <button
                                                onClick={() => setShowPreview(!showPreview)}
                                                className="flex items-center gap-1.5 text-2xs font-bold text-text-faint hover:text-text transition-colors px-2 py-1"
                                            >
                                                {showPreview ? <EyeOff className="h-3 w-3" /> : <Eye className="h-3 w-3" />}
                                                {showPreview ? 'EDIT' : 'PREVIEW'}
                                            </button>
                                        </div>
                                        {showPreview ? (
                                            <div className="w-full flex-1 min-h-[200px] max-h-[500px] overflow-y-auto bg-bg border border-border p-4 text-sm text-text leading-relaxed rounded-md">
                                                {renderTextContent(draftAgent.system_prompt || '*No system prompt yet.*')}
                                            </div>
                                        ) : (
                                            <VaultTextarea
                                                value={draftAgent.system_prompt}
                                                onChange={e => setDraftAgent({ ...draftAgent, system_prompt: e.target.value })}
                                                className="w-full flex-1 min-h-[200px] bg-bg border border-border p-3 text-xs font-mono text-text focus:border-border-strong focus:outline-none resize-none leading-relaxed rounded-md"
                                                placeholder="You are a helpful assistant. Type @ to reference a vault file..."
                                            />
                                        )}
                                    </div>
                                </div>
                            )} {/* end agentSubTab === 'config' */}

                            {/* ── Messaging Channels sub-tab ─────────────────── */}
                            {agentSubTab === 'messaging' && (
                                <div className="space-y-4">
                                    <p className="text-2xs text-text-faint">
                                        Channels this agent answers on. A channel can front several agents,
                                        and an agent can answer on several channels — assign them from{' '}
                                        <strong className="text-text">Settings → Messaging</strong>.
                                    </p>
                                    {agentChannels.length === 0 ? (
                                        <div className="p-8 border border-dashed border-border text-center text-text-faint space-y-3">
                                            <MessageSquare className="h-8 w-8 mx-auto opacity-20" />
                                            <p className="text-xs">This agent is not assigned to any channel yet.</p>
                                            <p className="text-2xs">Go to <strong className="text-text-muted">Settings → Messaging</strong>, pick a channel and add this agent to it.</p>
                                        </div>
                                    ) : (
                                        <div className="space-y-2">
                                            {agentChannels.map((ch: any) => {
                                                const EMOJI: Record<string, string> = { telegram: '✈️', discord: '🎮', slack: '💬', teams: '📘', whatsapp: '📱' };
                                                return (
                                                    <div key={ch.id} className="flex items-center gap-3 p-3 border border-border bg-bg rounded-md">
                                                        <span className="text-lg">{EMOJI[ch.platform] ?? '🤖'}</span>
                                                        <div className="flex-1 min-w-0">
                                                            <div className="text-xs font-bold text-text">{ch.name}</div>
                                                            <div className="text-2xs text-text-faint capitalize">
                                                                {ch.platform}
                                                                {ch.agent_ids?.[0] === draftAgent.id ? ' · answers by default' : ''}
                                                                {(ch.agent_ids?.length ?? 0) > 1 ? ` · ${ch.agent_ids.length} agents` : ''}
                                                                {ch.multi_agent_mode ? ' · switchable' : ''}
                                                            </div>
                                                        </div>
                                                        {ch.status === 'running'
                                                            ? <span className="flex items-center gap-1 text-2xs text-success"><CheckCircle className="h-3 w-3" /> Running</span>
                                                            : ch.status === 'error'
                                                                ? <span className="flex items-center gap-1 text-2xs text-danger"><XCircle className="h-3 w-3" /> Error</span>
                                                                : <span className="flex items-center gap-1 text-2xs text-text-faint"><Square className="h-3 w-3" /> Stopped</span>
                                                        }
                                                    </div>
                                                );
                                            })}
                                        </div>
                                    )}
                                </div>
                            )}

                            {/* ── Runs / Metrics / Used by ───────────────────── */}
                            {(agentSubTab === 'runs' || agentSubTab === 'metrics' || agentSubTab === 'usedby') && (
                                <AgentActivity
                                    /* Keyed by agent: switching agents remounts the tab, so its
                                       loading state is its initial state rather than a reset. */
                                    key={draftAgent.id}
                                    agentId={draftAgent.id}
                                    tab={agentSubTab}
                                    channels={agentChannels}
                                    agents={agents}
                                />
                            )}
                        </>)}
                    </div>
                ) : (
                    <div className="h-full flex flex-col items-center justify-center text-text-faint space-y-4">
                        <Bot className="h-12 w-12 opacity-20" />
                        <p className="text-sm">Select an agent to edit or create a new one.</p>
                    </div>
                )}
            </div>
        </div>
    );
};
