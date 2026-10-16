/* eslint-disable @typescript-eslint/no-explicit-any */
import { AlertTriangle, Bot, Trash, Copy, ChevronDown, ChevronRight, Lock, Sparkles, MessageSquare, ExternalLink, CheckCircle, XCircle, Square, PanelRightOpen, Image as ImageIcon } from 'lucide-react';
import { Button, Checkbox, Combobox, Drawer, ErrorNote, Input, Label, MarkdownEditor, Modal, SearchInput, Section, Select, Spinner, Switch } from '@/components/ui';
import Link from 'next/link';
import { matchesQuery } from '@/lib/search';
import { humanizeToolName } from '@/lib/toolNames';
import { toolContext } from '@/lib/toolTokens';
import { HELPERS_TOOL, selectedToolNames, sizedTools } from './hooks/useCapabilities';
import { vaultMentions } from '@/components/VaultMention';
import { CAPABILITIES, AUTO_TOOLS_BY_TYPE } from './types';
import { useDispatch } from 'react-redux';
import { AppDispatch } from '@/store';
import { addAgent, updateAgent } from '@/store/settingsSlice';
import { ToastNotification } from './ToastNotification';
import { AgentActivity, ActivityTab, ThisMonth, WhereItWorks } from './AgentActivity';
import { AgentTest, toolNamesOf, type AgentStatus } from './AgentTest';
import { AgentMemory } from './AgentMemory';
import { ToolPolicyFields, gatingCategories, POLICY_CATEGORIES, toolRules, type ToolPolicy } from './ToolPolicy';
import { KnowledgeBasePicker } from '@/components/knowledge/KnowledgeBasePicker';
import { AgentBoards } from '@/components/boards/AgentBoards';
import { TeamSetup, newTeamBlock } from '@/components/team';
import { useNavFlags } from '@/components/app/NavFlags';
import { OssBoardsHost } from '@/lib/boardsHost';
import { knowledgeApi, useKnowledgeEnabled, type KnowledgeBase } from '@/lib/knowledge';
import { cn } from '@/lib/cn';
import { providerLabel } from '@/lib/models';

/** The longest name the server accepts (core/models.py `AGENT_NAME_MAX`, F217). */
const AGENT_NAME_MAX = 100;

interface AgentsTabProps {
    agents: any[];
    selectedAgentId: string | null;
    setSelectedAgentId: (id: string | null) => void;
    draftAgent: any;
    /** An edit to the open draft. */
    setDraftAgent: (agent: any) => void;
    /** Open a draft: it becomes both the draft and what "unsaved" is measured from. */
    loadDraftAgent: (agent: any) => void;
    /** Run `go` now, or once the person agrees to lose an unsaved edit (F73). */
    guard: (go: () => void) => void;
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
    /** Of those, the ones with no "off" — the Claude CLI, which runs off as low.
     *  Served for the same reason as the list above. */
    reasoningWithoutOff?: string[];
    /** Where Off is the model's lowest thinking level rather than none (F269). */
    reasoningOffMinimum?: string[];
    defaultModel?: string;
    /** The workspace's "Model for quick steps" (empty when none is set): a choice for "Helper model". */
    quickModel?: string;
    /** The workspace's `reasoning_effort`, which an agent with none of its own
     *  runs at. Absent when the workspace never set one. */
    workspaceEffort?: string;
    loadingAgents?: boolean;
    /** The open draft differs from what was opened or saved (F73). */
    dirty: boolean;
    /** Put the draft back as it was opened or saved, after asking (the Save bar's Discard). */
    onDiscard: () => void;
    /** What `/api/status` says about each agent: whether it sees images. */
    agentStatus: Record<string, AgentStatus>;
    /** The test chat's column is open (wide screens); the person's choice, remembered. */
    testOpen: boolean;
    setTestOpen: (open: boolean) => void;
    /** "Build with AI" for one agent: opened from New agent's menu (M2d, Naveen). */
    aiBuilderOpen: boolean;
    setAiBuilderOpen: (open: boolean) => void;
}

import React, { useState, useEffect } from 'react';


export const AgentsTab = ({
    agents, selectedAgentId, setSelectedAgentId,
    draftAgent, setDraftAgent, loadDraftAgent, guard, availableCapabilities, loadingCapabilities = false, customTools,
    onDeleteAgent, providers, reasoningCapable, reasoningWithoutOff, reasoningOffMinimum, defaultModel, quickModel = '', workspaceEffort, loadingAgents = false,
    dirty, onDiscard, agentStatus, testOpen, setTestOpen, aiBuilderOpen, setAiBuilderOpen,
}: AgentsTabProps) => {
    const dispatch = useDispatch<AppDispatch>();
    const [repos, setRepos] = useState<any[]>([]);
    const [allSkills, setAllSkills] = useState<any[]>([]);
    const knowledgeOn = useKnowledgeEnabled();
    const boardsOn = !!useNavFlags().boards_enabled;
    const [allBases, setAllBases] = useState<KnowledgeBase[]>([]);
    const [dbConfigs, setDbConfigs] = useState<any[]>([]);
    const [agentTypes, setAgentTypes] = useState<{ value: string; label: string; description: string }[]>([]);
    const [expandedCaps, setExpandedCaps] = useState<Set<string>>(new Set());
    const [promptDescription, setPromptDescription] = useState('');
    const [isGenerating, setIsGenerating] = useState(false);
    // Why the Prompt Writer wrote nothing, shown under its field (F183). Kept
    // with the agent it was for, so opening another agent does not show it.
    const [promptFailure, setPromptFailure] = useState<{ agentId: string; text: string } | null>(null);
    const promptError = promptFailure && promptFailure.agentId === (draftAgent?.id ?? '') ? promptFailure.text : null;
    const setPromptError = (text: string | null) =>
        setPromptFailure(text === null ? null : { agentId: draftAgent?.id ?? '', text });
    const [agentSubTab, setAgentSubTab] = useState<'config' | 'messaging' | ActivityTab>('config');
    const [agentChannels, setAgentChannels] = useState<any[]>([]);
    // The workspace floor an unset category inherits, and the widgets that
    // publish this agent — both read-only here, both needed to say what a
    // choice will actually do before it is saved.
    const [policyDefaults, setPolicyDefaults] = useState<ToolPolicy>({});
    // The most members any Team agent here may run at once (Settings → General; TEAM1b, Q2a).
    const [teamCeiling, setTeamCeiling] = useState<number>(10);
    const [widgets, setWidgets] = useState<any[]>([]);
    const [isSaving, setIsSaving] = useState(false);
    const [toast, setToast] = useState<{ show: boolean; message: string; type: 'success' | 'error' } | null>(null);
    const [toolQuery, setToolQuery] = useState('');
    // Tools the agent names that nothing here provides (F526): run without them, so said here.
    const [unknownTools, setUnknownTools] = useState<{ name: string; nearest: string[] }[]>([]);
    const [aiBuilderDesc, setAiBuilderDesc] = useState('');
    const [agentQuery, setAgentQuery] = useState('');
    const [isBuilding, setIsBuilding] = useState(false);

    const showToast = (message: string, type: 'success' | 'error') => {
        setToast({ show: true, message, type });
        setTimeout(() => setToast(null), 4000);
    };

    const handleDuplicate = (agent: any, e: React.MouseEvent) => {
        e.stopPropagation();
        guard(() => { void duplicate(agent); });
    };

    const duplicate = async (agent: any) => {
        // The original is clipped, not the marker, so a copy of a long name still says so.
        const copy = { ...agent, id: `agent_${Date.now()}`, name: `${String(agent.name).slice(0, AGENT_NAME_MAX - 7).trimEnd()} (Copy)` };
        delete copy.created_at;
        delete copy.updated_at;
        try {
            const res = await fetch('/api/agents', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(copy),
            });
            if (res.ok) {
                const { unknown_tools, ...saved } = await res.json();
                setUnknownTools(Array.isArray(unknown_tools) ? unknown_tools : []);
                dispatch(addAgent(saved));
                setSelectedAgentId(saved.id);
                loadDraftAgent(saved);
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
                loadDraftAgent(saved);
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
        // An agent saved before the limit can hold a longer name; say so rather
        // than let the server's 422 read as "Failed to save agent".
        if ((draftAgent.name || '').length > AGENT_NAME_MAX) {
            showToast(`An agent's name is at most ${AGENT_NAME_MAX} characters.`, 'error');
            return;
        }
        setIsSaving(true);
        try {
            const res = await fetch('/api/agents', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(draftAgent),
            });
            if (res.ok) {
                const { unknown_tools, ...saved } = await res.json();
                setUnknownTools(Array.isArray(unknown_tools) ? unknown_tools : []);
                const isNew = !agents.some((a: any) => a.id === draftAgent.id);
                if (isNew) {
                    dispatch(addAgent(saved));
                    setSelectedAgentId(saved.id);
                } else {
                    dispatch(updateAgent(saved));
                }
                // The route answers with what it stored, which is not always what
                // was sent (a tool policy is cleaned). That is the new baseline.
                loadDraftAgent(saved);
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

    const [testDrawer, setTestDrawer] = useState(false);

    // An agent opened here asks which of its tools nothing provides (F526): one saved by a script or the API never
    // went through this page's save, and Harbor's `web_search` went unnoticed for two sessions.
    const openedId = draftAgent?.id;
    useEffect(() => {
        const tools: string[] = (draftAgent?.tools || []).filter((t: string) => t !== 'all' && !t.startsWith('group:'));
        if (!openedId || !tools.length) { setUnknownTools([]); return; }
        let gone = false;
        fetch('/api/agents/tool-check', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tools }),
        })
            .then(r => (r.ok ? r.json() : { unknown_tools: [] }))
            .then(body => { if (!gone) setUnknownTools(Array.isArray(body.unknown_tools) ? body.unknown_tools : []); })
            .catch(() => { if (!gone) setUnknownTools([]); });
        return () => { gone = true; };
        // Asked when an agent opens, not on every tick of a box: a save answers for the rest.
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [openedId]);

    // Reset the sub-tab whenever the selected agent changes, and read its
    // channels: the setup's "Where it works" names them, as the two tabs do.
    const savedAgent = draftAgent ? agents.find((a: any) => a.id === draftAgent.id) ?? null : null;
    const savedId: string | null = savedAgent?.id ?? null;
    const loadChannels = (id: string) => {
        fetch(`/api/messaging/channels?agent_id=${encodeURIComponent(id)}`)
            .then(r => r.ok ? r.json() : [])
            .then(d => setAgentChannels(Array.isArray(d) ? d : []))
            .catch(() => setAgentChannels([]));
    };
    useEffect(() => {
        setAgentSubTab('config');
        setAgentChannels([]);
        if (savedId) loadChannels(savedId);
    }, [selectedAgentId, savedId]);

    useEffect(() => {
        if (!knowledgeOn) return;
        let live = true;
        knowledgeApi.bases()
            .then(list => { if (live) setAllBases(list); })
            .catch(err => console.error('Failed to fetch knowledge bases', err));
        return () => { live = false; };
    }, [knowledgeOn]);

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
            .then(data => {
                setPolicyDefaults(data?.tool_policy_defaults || {});
                const ceiling = Number(data?.team_at_once_ceiling);
                if (Number.isFinite(ceiling) && ceiling >= 1) setTeamCeiling(Math.floor(ceiling));
            })
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
        if (draftAgent.tools.includes("all")) {
            // "all" holds every group, so the only change is switching this one
            // off: the explicit list minus this group. Asking whether the group's
            // own names were listed read false, re-added them beside "all", and
            // the box could never be turned off (F235).
            const allToolsFlat = availableCapabilities.flatMap((c: any) => c.tools);
            const newTools = [...allToolsFlat.filter((t: string) => !cap.tools.includes(t)), ...ownSwitches(draftAgent.tools)];
            setDraftAgent({ ...draftAgent, tools: newTools });
        } else {
            const allGroupEnabled = cap.tools.every((t: string) => draftAgent.tools.includes(t));
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
            const newTools = [...allToolsFlat.filter((t: string) => t !== toolName), ...ownSwitches(draftAgent.tools)];
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
        setPromptError(null);
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
                    // What it answers from: the writer replaces its "use your own
                    // knowledge" rule for such an agent (B1b).
                    knowledge_bases: allBases
                        .filter(kb => (draftAgent.knowledge || []).includes(kb.id))
                        .map(kb => ({ name: kb.name, description: kb.description || '' })),
                }),
                signal: AbortSignal.timeout(180_000), // 3 minutes timeout for LLM generation
            });
            if (!res.ok) {
                // A model that cannot answer is a 502 with the provider's words; say
                // them here rather than only in the console (F183).
                const failure = await res.json().catch(() => ({}));
                const detail = typeof failure?.detail === 'string' ? failure.detail : '';
                setPromptError(detail || `The prompt writer failed (${res.status}). The prompt was not changed.`);
                return;
            }
            const data = await res.json();
            setDraftAgent({ ...draftAgent, system_prompt: data.system_prompt });
            setPromptDescription('');
        } catch (err) {
            console.error('Failed to generate prompt:', err);
            setPromptError(err instanceof DOMException && err.name === 'TimeoutError'
                ? 'The model took more than 3 minutes and was stopped. The prompt was not changed.'
                : 'The prompt writer could not be reached. The prompt was not changed.');
        } finally {
            setIsGenerating(false);
        }
    };

    const visibleAgents = Array.isArray(agents)
        ? agents.filter((a: any) => matchesQuery(agentQuery, a.name, a.description, a.id))
        : [];

    const isNew = !!draftAgent && !savedAgent;
    const typeLabel = (type?: string) => {
        const t = type || 'conversational';
        return agentTypes.find(x => x.value === t)?.label || t.charAt(0).toUpperCase() + t.slice(1);
    };
    const knowledgeNames = (savedAgent?.knowledge || [])
        .map((id: string) => allBases.find(kb => kb.id === id)?.name)
        .filter(Boolean) as string[];
    const status = savedId ? agentStatus[savedId] : undefined;

    // The test chat: a column beside the setup on a wide screen, a drawer on a
    // narrow one. It always tests the agent as saved (D211).
    const test = (onCollapse?: () => void, className?: string) => (
        <AgentTest
            agent={savedAgent}
            dirty={dirty}
            onSave={handleSaveAgent}
            saving={isSaving}
            status={status}
            toolNames={toolNamesOf(savedAgent)}
            knowledgeNames={knowledgeNames}
            onCollapse={onCollapse}
            className={className}
        />
    );

    const tabs: { id: 'config' | 'messaging' | ActivityTab; label: string }[] = [
        { id: 'config', label: 'Setup' },
        { id: 'messaging', label: 'Messaging Channels' },
        { id: 'runs', label: 'Runs' },
        { id: 'metrics', label: 'Metrics' },
        { id: 'usedby', label: 'Used by' },
    ];

    return (
        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
            {toast && <ToastNotification show={toast.show} message={toast.message} type={toast.type} />}

            {/* ── The list ─────────────────────────────────────────────── */}
            <aside aria-label="Your agents" className="flex max-h-[40vh] shrink-0 flex-col border-b border-border md:max-h-none md:w-64 md:border-b-0 md:border-r">
                {Array.isArray(agents) && agents.length > 4 && (
                    <div className="px-3 pb-2 pt-3">
                        <SearchInput
                            value={agentQuery}
                            onChange={setAgentQuery}
                            placeholder="Search agents…"
                        />
                    </div>
                )}

                <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 py-3 modern-scrollbar">
                    {loadingAgents && agents.length === 0 && (
                        <div className="flex items-center gap-2 px-2 py-4 text-sm text-text-faint">
                            <Spinner size={16} />
                            Loading agents…
                        </div>
                    )}
                    {Array.isArray(agents) && visibleAgents.length === 0 && agentQuery && (
                        <div className="py-6 text-center text-sm text-text-faint">
                            No agents match “{agentQuery}”.
                        </div>
                    )}
                    {visibleAgents.map((a: any) => {
                        const selected = selectedAgentId === a.id;
                        return (
                            // The row is a button, so a keyboard opens an agent as a click
                            // does (F219); Duplicate and Delete are its siblings, shown
                            // whenever the row has focus as well as on hover.
                            <div key={a.id} className="group relative">
                                <button
                                    type="button"
                                    onClick={() => {
                                        if (a.id === selectedAgentId) return;
                                        guard(() => {
                                            setSelectedAgentId(a.id);
                                            loadDraftAgent({ ...a });
                                        });
                                    }}
                                    aria-current={selected ? 'true' : undefined}
                                    title={a.description || undefined}
                                    className={cn(
                                        'flex w-full items-center gap-2.5 rounded-md px-2 py-2 pr-14 text-left transition-colors',
                                        selected ? 'bg-surface-2 ring-1 ring-inset ring-border' : 'hover:bg-row-hover',
                                    )}
                                >
                                    <span aria-hidden className={cn(
                                        'flex size-7 shrink-0 items-center justify-center rounded-md text-2xs font-semibold',
                                        selected ? 'bg-accent text-accent-fg' : 'bg-surface-2 text-text-muted',
                                    )}>
                                        {a.name.substring(0, 2).toUpperCase()}
                                    </span>
                                    <span className="block min-w-0 flex-1">
                                        <span className="block truncate text-sm font-medium text-text">{a.name}</span>
                                        <span className="block truncate text-xs text-text-faint">
                                            {a.type === 'orchestrator' ? 'Workflow' : (a.model || 'Default model')} · {typeLabel(a.type)}
                                        </span>
                                    </span>
                                </button>
                                <button
                                    type="button"
                                    onClick={(e) => handleDuplicate(a, e)}
                                    className="absolute right-7 top-1/2 -translate-y-1/2 rounded p-1 text-text-faint opacity-0 transition-opacity hover:text-text group-hover:opacity-100 group-focus-within:opacity-100"
                                    aria-label={`Duplicate ${a.name}`}
                                    title="Duplicate agent"
                                >
                                    <Copy className="size-3.5" />
                                </button>
                                <button
                                    type="button"
                                    onClick={(e) => {
                                        e.stopPropagation();
                                        onDeleteAgent(a.id);
                                    }}
                                    aria-label={`Delete ${a.name}`}
                                    title={`Delete ${a.name}`}
                                    className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-text-faint opacity-0 transition-opacity hover:text-danger group-hover:opacity-100 group-focus-within:opacity-100"
                                >
                                    <Trash className="size-3.5" />
                                </button>
                            </div>
                        );
                    })}
                </div>
            </aside>

            {/* One agent from a description (POST /api/agents/build): New agent's
                menu opens it (M2d). The rail's Build with AI is the workflow builder. */}
            <Modal
                open={aiBuilderOpen}
                onClose={() => setAiBuilderOpen(false)}
                title="Build with AI"
                description="Describe the agent; the AI picks its tools and writes its instructions, and it opens here."
                footer={(
                    <>
                        <Button variant="secondary" onClick={() => setAiBuilderOpen(false)}>Cancel</Button>
                        <Button
                            // The built agent replaces the open draft, so this is a switch too.
                            onClick={() => guard(() => { void buildAgentWithAI(); })}
                            disabled={isBuilding || !aiBuilderDesc.trim()}
                        >
                            {isBuilding
                                ? <><Spinner size={12} /> BUILDING…</>
                                : <><Sparkles className="size-3.5" aria-hidden /> CREATE AGENT</>}
                        </Button>
                    </>
                )}
            >
                <label id="agent-ai-builder-label" htmlFor="agent-ai-builder" className="sr-only">Build with AI</label>
                <textarea
                    id="agent-ai-builder"
                    aria-labelledby="agent-ai-builder-label"
                    value={aiBuilderDesc}
                    onChange={e => setAiBuilderDesc(e.target.value)}
                    onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); guard(() => { void buildAgentWithAI(); }); } }}
                    placeholder="Describe what this agent should do... e.g. 'A customer support agent that searches our knowledge base'"
                    rows={4}
                    autoFocus
                    className="w-full resize-none rounded-md border border-border bg-surface p-3 text-sm text-text placeholder:text-text-faint focus:border-accent focus:outline-none"
                />
            </Modal>

            {/* ── The setup ────────────────────────────────────────────── */}
            <div className="@container flex min-h-0 min-w-0 flex-1 flex-col">
                {draftAgent ? (<>
                    {/* `relative`: the setup's screen-reader-only labels position against
                        it, not the document, which they would stretch (M2a's trap). */}
                    <div className="relative min-h-0 flex-1 overflow-y-auto modern-scrollbar">
                        <div className="px-4 pb-10 pt-5 md:px-8">
                            {/* ── Orchestration agents are read-only here ─────────── */}
                            {draftAgent.type === 'orchestrator' ? (
                                <div className="flex min-h-[360px] flex-col items-center justify-center gap-5 text-center">
                                    <span className="relative flex size-16 items-center justify-center rounded-2xl bg-machine-subtle text-machine">
                                        <svg className="size-8" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5} aria-hidden>
                                            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
                                        </svg>
                                        <span className="absolute -right-1 -top-1 flex size-5 items-center justify-center rounded-full border border-border bg-surface">
                                            <Lock className="size-2.5 text-text-muted" aria-hidden />
                                        </span>
                                    </span>
                                    <div className="space-y-2">
                                        <h4 className="text-base font-semibold text-text">{draftAgent.name}</h4>
                                        <p className="max-w-[340px] text-sm leading-relaxed text-text-muted">
                                            This agent <span className="font-semibold text-text">runs a workflow</span>. Its steps and configuration are edited in Workflows.
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-2 rounded-md border border-border bg-surface-2 px-4 py-2.5 text-xs text-text-muted">
                                        <ExternalLink className="size-3.5 shrink-0" aria-hidden />
                                        <span>Open <strong className="text-text">Workflows</strong> to edit this agent&apos;s workflow</span>
                                    </div>
                                    <Button variant="secondary" size="sm" className="lg:hidden" onClick={() => setTestDrawer(true)}>
                                        Try {draftAgent.name}
                                    </Button>
                                </div>
                            ) : (<>
                                {/* The agent: who it is */}
                                <div className="flex items-end gap-3">
                                    <div className="grid min-w-0 flex-1 gap-3 @xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
                                        <div className="space-y-1">
                                            <Label htmlFor="age-draftagent-name" className="block">Name</Label>
                                            <Input id="age-draftagent-name"
                                                type="text"
                                                maxLength={AGENT_NAME_MAX}
                                                value={draftAgent.name}
                                                onChange={e => setDraftAgent({ ...draftAgent, name: e.target.value })}
                                            />
                                        </div>
                                        <div className="space-y-1">
                                            <Label htmlFor="age-draftagent-description" className="block">Description</Label>
                                            <Input id="age-draftagent-description"
                                                type="text"
                                                value={draftAgent.description}
                                                onChange={e => setDraftAgent({ ...draftAgent, description: e.target.value })}
                                            />
                                        </div>
                                    </div>
                                    <Button variant="secondary" className="shrink-0 lg:hidden" onClick={() => setTestDrawer(true)}>
                                        Try it
                                    </Button>
                                </div>
                                {isNew && <p className="mt-2 text-xs text-text-faint">New agent: not saved yet.</p>}

                                {/* Sub-tab row */}
                                <div role="tablist" aria-label="Agent" className="mt-5 flex flex-wrap items-center gap-1.5">
                                    {tabs.map(t => (
                                        <button
                                            key={t.id}
                                            type="button"
                                            role="tab"
                                            aria-selected={agentSubTab === t.id}
                                            onClick={() => {
                                                setAgentSubTab(t.id);
                                                // "Used by" names the agent's channels too, so both tabs
                                                // want the same fetch.
                                                if ((t.id === 'messaging' || t.id === 'usedby') && savedId) loadChannels(savedId);
                                            }}
                                            // The Runs page's pills (Naveen, M2d).
                                            className={cn(
                                                'inline-flex h-[30px] shrink-0 items-center whitespace-nowrap rounded-full border px-3 text-[13px] font-medium transition-colors',
                                                agentSubTab === t.id
                                                    ? 'border-transparent bg-text text-bg'
                                                    : 'border-border text-text-muted hover:border-border-strong hover:text-text',
                                            )}
                                        >
                                            {t.label}
                                        </button>
                                    ))}
                                </div>

                                {/* ── Setup ─────────────────────────────────────── */}
                                {agentSubTab === 'config' && (
                                    <div role="tabpanel" aria-label="Setup" className="mt-2">
                                        <SetupSection id="setup-model" title="Model">
                                            <div className="grid gap-x-6 gap-y-4 @xl:grid-cols-2">
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
                                                                // A new Team agent starts as a team (Q1); one that has its block keeps it.
                                                                ...(newType === 'delegate' && !draftAgent.team ? { team: newTeamBlock() } : {}),
                                                            });
                                                        }}
                                                    />
                                                    <p className="mt-1 text-xs text-text-faint">
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
                                                                        group: providerLabel(providerKey),
                                                                    }))),
                                                        ]}
                                                    />
                                                    <p className="mt-1 text-xs text-text-faint">Override the default model for this agent. Leave empty to use the system default.</p>
                                                </div>
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
                                                    {/* A dropdown (Naveen, M2d); it was a row of buttons. No Off
                                                        where the model has none: an Off that quietly runs as Low is
                                                        the "set and ignored" reading this control exists to avoid (F65). */}
                                                    {(() => {
                                                        const withoutOff = (reasoningWithoutOff ?? []).includes(draftAgent.model || defaultModel || '');
                                                        // What an agent with no effort of its own runs at (F99): the
                                                        // workspace's setting, else the engine's default — Low for the
                                                        // Claude CLI (F65), Medium for the rest. Off shows as Low where
                                                        // the model has none. The CLI by its prefix, not by "has no Off":
                                                        // a Gemini 3 Pro and a Claude 5 have no Off either (F269), and
                                                        // with nothing set they run at Medium.
                                                        const cli = (draftAgent.model || defaultModel || '').startsWith('cli.');
                                                        const set = (workspaceEffort || '').trim().toLowerCase();
                                                        const inherited = ['off', 'low', 'medium', 'high'].includes(set)
                                                            ? (set === 'off' && withoutOff ? 'low' : set)
                                                            : (cli ? 'low' : 'medium');
                                                        const stored = draftAgent.reasoning_effort || null;
                                                        const shown = stored === 'off' && withoutOff ? 'low' : stored;
                                                        const cap = (w: string) => w.charAt(0).toUpperCase() + w.slice(1);
                                                        return (
                                                            <Select
                                                                className="max-w-xs"
                                                                aria-label="Reasoning"
                                                                value={shown ?? '__default__'}
                                                                // `null`, not undefined, for the default: a stored agent reads
                                                                // back `null`, and the unsaved-changes guard compares JSON (F73).
                                                                onChange={v => setDraftAgent({ ...draftAgent, reasoning_effort: v === '__default__' ? null : v })}
                                                                options={[
                                                                    { value: '__default__', label: `Default · ${cap(inherited)}` },
                                                                    ...(withoutOff ? ['low', 'medium', 'high'] : ['off', 'low', 'medium', 'high'])
                                                                        .map(level => ({ value: level, label: cap(level) })),
                                                                ]}
                                                            />
                                                        );
                                                    })()}
                                                    <p className="mt-1 text-xs text-text-faint">How much this model thinks before it answers. Higher costs more and takes longer; thinking tokens are billed as output.</p>
                                                    {/* What Off does on this model, served by the models
                                                        endpoint rather than restated here (F269). */}
                                                    {(reasoningOffMinimum ?? []).includes(draftAgent.model || defaultModel || '') && (
                                                        <p className="mt-1 text-xs text-text-muted">This model can&apos;t stop thinking entirely: Off runs at its lowest thinking level.</p>
                                                    )}
                                                    {(reasoningWithoutOff ?? []).includes(draftAgent.model || defaultModel || '') && (
                                                        <p className="mt-1 text-xs text-text-muted">This model has no Off: the least it thinks is Low.</p>
                                                    )}
                                                </div>
                                            )}
                                        </SetupSection>

                                        <SetupSection id="setup-instructions" title="Instructions">
                                            {/* Prompt Generator — above the editor, because it writes what goes in it (Naveen, M2d). */}
                                            <div className="space-y-2 rounded-lg border border-border bg-surface-2/50 p-3">
                                                <Label htmlFor="agn-prompt-description" className="flex items-center gap-1.5">
                                                    <Sparkles className="size-3.5 text-machine" aria-hidden /> AI Prompt Writer
                                                </Label>
                                                <div className="flex gap-2">
                                                    <Input
                                                        id="agn-prompt-description"
                                                        type="text"
                                                        value={promptDescription}
                                                        onChange={e => setPromptDescription(e.target.value)}
                                                        onKeyDown={e => e.key === 'Enter' && !isGenerating && generatePrompt()}
                                                        placeholder="Describe what this agent should do... e.g. 'A customer support agent for a SaaS product'"
                                                        className="flex-1"
                                                    />
                                                    <Button
                                                        variant="secondary"
                                                        onClick={generatePrompt}
                                                        disabled={isGenerating || !promptDescription.trim()}
                                                    >
                                                        {isGenerating ? (
                                                            <><Spinner size={12} /> GENERATING...</>
                                                        ) : (
                                                            <><Sparkles className="size-3.5" aria-hidden /> GENERATE</>
                                                        )}
                                                    </Button>
                                                </div>
                                                <p className="text-xs text-text-faint">Describe the agent&apos;s purpose and the AI will generate a comprehensive system prompt. Tools and date/time context are auto-injected at runtime.</p>
                                                {promptError && (
                                                    <ErrorNote className="text-xs">
                                                        {promptError}{' '}
                                                        <Link href="/settings/models" className="whitespace-nowrap font-medium underline underline-offset-2">Settings → Models</Link>
                                                    </ErrorNote>
                                                )}
                                            </div>
                                            {/* The kit's markdown editor. `global` because a system
                                                prompt fills in `{{ global.key }}` and nothing else; the
                                                vault's `@` rides the same suggestion list. */}
                                            <div className="space-y-1">
                                                <Label htmlFor="agent-system-prompt" className="sr-only">Instructions</Label>
                                                <MarkdownEditor
                                                    key={draftAgent.id || 'new'}
                                                    id="agent-system-prompt"
                                                    rows={14}
                                                    profile="global"
                                                    mentions={vaultMentions}
                                                    value={draftAgent.system_prompt || ''}
                                                    onChange={system_prompt => setDraftAgent({ ...draftAgent, system_prompt })}
                                                    placeholder="You are a helpful assistant. Type {{ for a variable, @ for a vault file…"
                                                    previewEmpty="No system prompt yet."
                                                />
                                                <p className="text-xs text-text-faint">
                                                    Type <code className="font-code">{'{{'}</code> for a variable — <code className="font-code">{'{{ global.key }}'}</code> is filled in each time the agent runs.
                                                </p>
                                            </div>

                                        </SetupSection>

                                        {draftAgent.type === 'delegate' && (
                                            /* ── A Team agent (stored `delegate`): its members and how they work (TEAM1b; Q1, Q2a, Q11) ── */
                                            <SetupSection id="setup-members" title="Members">
                                                <TeamSetup
                                                    block={draftAgent.team}
                                                    onChange={team => setDraftAgent({ ...draftAgent, team })}
                                                    ceiling={teamCeiling}
                                                    ceilingNote={`Up to ${teamCeiling} on this install (Settings → General). Others wait for a free slot.`}
                                                    members={(
                                                <div className="space-y-3">
                                                    <p className="text-xs text-text-faint">
                                                        The agents this Team agent can hand work to. Each works with its own tools and approvals. Leave all unticked to allow any agent.
                                                    </p>
                                                    {(() => {
                                                        const otherAgents = agents.filter((a: any) =>
                                                            a.id !== draftAgent.id && a.type !== 'builder'
                                                        );
                                                        const selectedIds: string[] = draftAgent.delegate_agent_ids || [];
                                                        const allSelected = selectedIds.length === 0;
                                                        if (otherAgents.length === 0) {
                                                            return (
                                                                <div className="rounded-lg border border-dashed border-border p-6 text-center text-text-faint">
                                                                    <Bot className="mx-auto mb-2 size-6 opacity-30" aria-hidden />
                                                                    <p className="text-xs">No other agents available. Create agents first, then assign them here.</p>
                                                                </div>
                                                            );
                                                        }
                                                        return (
                                                            <div className="space-y-2">
                                                                {/* All agents: ticked while no agent is picked. Ticking it
                                                                    clears the picks; it is unticked by picking agents below,
                                                                    so while ticked it is disabled rather than a box that
                                                                    refuses to untick (F184). */}
                                                                <label
                                                                    className={cn(
                                                                        'flex items-center gap-3 rounded-lg border p-3 transition-colors',
                                                                        allSelected ? 'border-border-strong bg-surface-2' : 'cursor-pointer border-border bg-surface hover:border-border-strong',
                                                                    )}
                                                                >
                                                                    <Checkbox
                                                                        checked={allSelected}
                                                                        disabled={allSelected}
                                                                        onChange={() => setDraftAgent({ ...draftAgent, delegate_agent_ids: [] })}
                                                                        label="All Agents"
                                                                        className="disabled:opacity-100"
                                                                    />
                                                                    <span className="block min-w-0 flex-1">
                                                                        <span className="block text-sm font-medium text-text">All Agents</span>
                                                                        <span className="block text-xs text-text-faint">Any agent here can be a member</span>
                                                                    </span>
                                                                    {allSelected && <span className="rounded-md bg-success-subtle px-1.5 py-0.5 text-2xs font-medium text-success">ACTIVE</span>}
                                                                </label>

                                                                {/* Individual agents */}
                                                                <div className="grid gap-2 @xl:grid-cols-2">
                                                                    {otherAgents.map((a: any) => {
                                                                        const isSelected = selectedIds.includes(a.id);
                                                                        return (
                                                                            <label
                                                                                key={a.id}
                                                                                className={cn(
                                                                                    'block cursor-pointer rounded-lg border p-3 transition-colors',
                                                                                    isSelected ? 'border-border-strong bg-surface-2' : allSelected ? 'border-border bg-surface opacity-60' : 'border-border bg-surface hover:border-border-strong',
                                                                                )}
                                                                            >
                                                                                <span className="flex items-center gap-2">
                                                                                    <Checkbox
                                                                                        checked={isSelected}
                                                                                        onChange={() => {
                                                                                            const newIds = isSelected
                                                                                                ? selectedIds.filter((id: string) => id !== a.id)
                                                                                                : [...selectedIds, a.id];
                                                                                            setDraftAgent({ ...draftAgent, delegate_agent_ids: newIds });
                                                                                        }}
                                                                                        label={a.name}
                                                                                    />
                                                                                    <span className="block min-w-0 flex-1">
                                                                                        <span className="block truncate text-sm font-medium text-text">{a.name}</span>
                                                                                        <span className="block truncate text-xs text-text-faint">{a.description}</span>
                                                                                    </span>
                                                                                    <span className="shrink-0 rounded-md bg-surface-2 px-1 text-2xs text-text-faint">{typeLabel(a.type)}</span>
                                                                                </span>
                                                                            </label>
                                                                        );
                                                                    })}
                                                                </div>
                                                            </div>
                                                        );
                                                    })()}
                                                </div>
                                                    )}
                                                />
                                            </SetupSection>
                                        )}

                                        <SetupSection id="setup-tools" title="Tools">
                                            <div className="space-y-3">
                                                <Label className="sr-only">Capabilities (Tools)</Label>
                                                {unknownTools.length > 0 && (
                                                    <div role="note" data-unknown-tools className="grid gap-1.5 rounded-md border border-person/40 bg-person-subtle px-3 py-2.5 text-sm text-text">
                                                        <span>
                                                            Nothing here provides {unknownTools.map((t, i) => (
                                                                <span key={t.name}>{i ? ', ' : ''}<span className="font-code">{t.name}</span>{t.nearest.length ? <span className="text-text-muted"> (nearest: {t.nearest.join(', ')})</span> : null}</span>
                                                            ))}, so this agent runs without {unknownTools.length > 1 ? 'them' : 'it'}.
                                                        </span>
                                                        <div>
                                                            <Button
                                                                size="sm"
                                                                variant="secondary"
                                                                onClick={() => {
                                                                    const drop = new Set(unknownTools.map(t => t.name));
                                                                    setDraftAgent({ ...draftAgent, tools: draftAgent.tools.filter((t: string) => !drop.has(t)) });
                                                                    setUnknownTools([]);
                                                                }}
                                                            >
                                                                Remove {unknownTools.length > 1 ? 'them' : 'it'} from this agent
                                                            </Button>
                                                        </div>
                                                    </div>
                                                )}
                                                {loadingCapabilities ? (
                                                    /* ── Skeleton loader ── */
                                                    <div className="grid gap-3 @2xl:grid-cols-2">
                                                        {Array.from({ length: 8 }).map((_, i) => (
                                                            <div key={i} className="animate-pulse space-y-2 rounded-lg border border-border bg-surface p-4">
                                                                <div className="flex items-center gap-2">
                                                                    <div className="size-3 rounded-sm bg-surface-2" />
                                                                    <div className="h-2.5 w-24 rounded-md bg-surface-2" />
                                                                </div>
                                                                <div className="ml-5 h-2 w-32 rounded-md bg-surface-2/70" />
                                                            </div>
                                                        ))}
                                                    </div>
                                                ) : (() => {
                                                    const agentType = draftAgent.type || 'conversational';
                                                    const autoTools = new Set([
                                                        ...(AUTO_TOOLS_BY_TYPE.all_types || []),
                                                        ...(AUTO_TOOLS_BY_TYPE[agentType] || []),
                                                    ]);
                                                    // One line per group (Naveen, M2d: the section was too tall);
                                                    // a group's description on hover and in its opened list. A
                                                    // search once there are more than 8, as cloud's editor has.
                                                    const q = toolQuery.trim();
                                                    const detailsOf = (cap: any): { name: string, description: string }[] =>
                                                        cap.toolDetails || cap.tools.map((t: string) => ({ name: t, description: '' }));
                                                    const shown = availableCapabilities
                                                        .map((cap: any) => ({
                                                            cap,
                                                            groupHit: !q || matchesQuery(q, cap.label, cap.description, cap.server),
                                                            tools: detailsOf(cap).filter(t => !q || matchesQuery(q, t.name, humanizeToolName(t.name), t.description)),
                                                        }))
                                                        .filter((x: any) => x.groupHit || x.tools.length > 0);
                                                    return (<>
                                                        {availableCapabilities.length > 8 && (
                                                            <SearchInput value={toolQuery} onChange={setToolQuery} placeholder="Search tools…" className="max-w-sm" />
                                                        )}
                                                        {q && shown.length === 0 && <p className="text-sm text-text-faint">Nothing matches that.</p>}
                                                        {/* A fixed height that scrolls, so the section stays short (Naveen, M2d). */}
                                                        <div className="max-h-[440px] overflow-y-auto rounded-lg border border-border bg-surface-2/40 p-2 modern-scrollbar">
                                                        <div className="grid items-start gap-2 @2xl:grid-cols-2">
                                                            {shown.map(({ cap, groupHit, tools: matched }: any) => {
                                                                const toolDetails: { name: string, description: string }[] = q && !groupHit ? matched : detailsOf(cap);
                                                                const hasMultipleTools = detailsOf(cap).length > 1;
                                                                // A search opens the groups it found tools in.
                                                                const isExpanded = expandedCaps.has(cap.id) || (!!q && !groupHit);
                                                                const isAutoGroup = cap.tools.every((t: string) => autoTools.has(t));
                                                                const enabledCount = cap.tools.filter((t: string) =>
                                                                    isAutoGroup || draftAgent.tools.includes("all") || draftAgent.tools.includes(t)
                                                                ).length;
                                                                const allGroupEnabled = enabledCount === cap.tools.length;
                                                                const someEnabled = enabledCount > 0 && !allGroupEnabled;
                                                                const badges = (
                                                                    <>
                                                                        {cap.toolType === 'custom' && <span className="rounded-md bg-surface-2 px-1 text-2xs text-text-muted">CUSTOM</span>}
                                                                        {cap.toolType === 'mcp' && <span className="rounded-md bg-surface-2 px-1 text-2xs text-text-muted">MCP</span>}
                                                                    </>
                                                                );

                                                                return (
                                                                    <div
                                                                        key={cap.id}
                                                                        className={cn(
                                                                            'rounded-lg border bg-surface transition-colors',
                                                                            allGroupEnabled || someEnabled ? 'border-border-strong' : 'border-border',
                                                                        )}
                                                                    >
                                                                        {/* A real control per group, so a keyboard can give an agent
                                                                            tools and a screen reader hears each one's state (F184). A
                                                                            group of several tools keeps its box and its expander apart:
                                                                            one ticks the whole group, the other opens it. A group of one
                                                                            is that one tool, so it is a switch (M2d). */}
                                                                        {isAutoGroup ? (
                                                                            <div className="flex items-center gap-2 px-3 py-2" title={`Included by default for ${agentType} agents`}>
                                                                                <Lock className="size-3 shrink-0 text-text-faint" aria-hidden />
                                                                                <span className="flex-1 truncate text-sm font-medium text-text">{cap.label}</span>
                                                                                <span className="rounded-md bg-surface-2 px-1.5 py-0.5 text-2xs text-text-muted">DEFAULT</span>
                                                                            </div>
                                                                        ) : hasMultipleTools ? (
                                                                            <div className="flex items-center gap-2.5 px-3 py-2" title={cap.description || undefined}>
                                                                                <Checkbox
                                                                                    checked={allGroupEnabled ? true : someEnabled ? 'indeterminate' : false}
                                                                                    onChange={() => toggleGroupTools(cap)}
                                                                                    label={cap.label}
                                                                                />
                                                                                <button
                                                                                    type="button"
                                                                                    onClick={() => toggleExpand(cap.id)}
                                                                                    aria-expanded={isExpanded}
                                                                                    aria-label={`${cap.label} tools, ${enabledCount} of ${cap.tools.length} on`}
                                                                                    className="flex min-w-0 flex-1 items-center gap-2 rounded-sm text-left"
                                                                                >
                                                                                    <span className="flex-1 truncate text-sm font-medium text-text">{cap.label}</span>
                                                                                    {badges}
                                                                                    <span className="text-xs tabular-nums text-text-faint">{enabledCount}/{cap.tools.length}</span>
                                                                                    {isExpanded
                                                                                        ? <ChevronDown className="size-3.5 shrink-0 text-text-faint" aria-hidden />
                                                                                        : <ChevronRight className="size-3.5 shrink-0 text-text-faint" aria-hidden />}
                                                                                </button>
                                                                            </div>
                                                                        ) : (
                                                                            <label className="flex cursor-pointer items-center gap-2.5 px-3 py-2" title={cap.description || undefined}>
                                                                                <span className="flex-1 truncate text-sm font-medium text-text">{cap.label}</span>
                                                                                {badges}
                                                                                <Switch
                                                                                    checked={allGroupEnabled}
                                                                                    onChange={() => toggleGroupTools(cap)}
                                                                                    label={cap.label}
                                                                                />
                                                                            </label>
                                                                        )}

                                                                        {!isExpanded && (
                                                                            <p className="-mt-1 line-clamp-2 px-3 pb-2 text-xs text-text-faint">
                                                                                {isAutoGroup ? `Included by default for ${agentType} agents` : cap.description}
                                                                            </p>
                                                                        )}

                                                                        {isExpanded && hasMultipleTools && !isAutoGroup && (
                                                                            <div className="divide-y divide-border border-t border-border px-3">
                                                                                {cap.description && <p className="py-2 text-xs text-text-faint">{cap.description}</p>}
                                                                                {toolDetails.map((tool: { name: string, description: string }) => {
                                                                                    const isToolAuto = autoTools.has(tool.name);
                                                                                    const isToolEnabled = isToolAuto || draftAgent.tools.includes("all") || draftAgent.tools.includes(tool.name);
                                                                                    const Row = isToolAuto ? 'div' : 'label';
                                                                                    return (
                                                                                        <Row
                                                                                            key={tool.name}
                                                                                            className={cn('flex items-start gap-3 py-2', isToolAuto ? 'cursor-default opacity-60' : 'cursor-pointer')}
                                                                                        >
                                                                                            <span className="block min-w-0 flex-1">
                                                                                                {/* Humanized name leads; the dispatch key stays beside it
                                                                                                    (small, mono) because it is what a step stores and what
                                                                                                    a run log prints. */}
                                                                                                <span className="block text-xs text-text">
                                                                                                    {humanizeToolName(tool.name)}
                                                                                                    <span className="ml-1.5 font-mono text-[10px] text-text-faint" translate="no">{tool.name}</span>
                                                                                                </span>
                                                                                                {tool.description && (
                                                                                                    <span className="mt-0.5 line-clamp-2 block text-xs leading-tight text-text-faint">{tool.description}</span>
                                                                                                )}
                                                                                            </span>
                                                                                            {isToolAuto ? (
                                                                                                <Lock className="mt-0.5 size-3 shrink-0 text-text-faint" aria-hidden />
                                                                                            ) : (
                                                                                                <Switch
                                                                                                    size="sm"
                                                                                                    checked={isToolEnabled}
                                                                                                    onChange={() => toggleSingleTool(tool.name, cap)}
                                                                                                    label={humanizeToolName(tool.name)}
                                                                                                    className="mt-0.5"
                                                                                                />
                                                                                            )}
                                                                                        </Row>
                                                                                    );
                                                                                })}
                                                                            </div>
                                                                        )}
                                                                    </div>
                                                                );
                                                            })}
                                                        </div>
                                                        </div>
                                                        {/* What these tools add to every model call, and the warning past
                                                            the budget "every tool" is held to at run time (F483). */}
                                                        {(() => {
                                                            const context = toolContext(
                                                                sizedTools(availableCapabilities),
                                                                selectedToolNames(availableCapabilities, draftAgent.tools),
                                                                draftAgent.tools.includes('all'),
                                                            );
                                                            if (!context.text) return null;
                                                            return (
                                                                <p className={cn('flex items-start gap-1.5 text-xs', context.warn ? 'text-warning' : 'text-text-faint')}>
                                                                    {context.warn && <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />}
                                                                    <span>{context.text}</span>
                                                                </p>
                                                            );
                                                        })()}
                                                    </>);
                                                })()}
                                            </div>

                                            {draftAgent.type === 'code' && (
                                                <div className="space-y-1">
                                                    <Label className="block">Linked Repositories</Label>
                                                    <div className={boxed}>
                                                        {repos.length === 0 && <span className="text-sm text-text-muted">No repositories indexed yet.</span>}
                                                        {repos.map(repo => {
                                                            const isLinked = draftAgent.repos?.includes(repo.id);
                                                            return (
                                                                <button
                                                                    key={repo.id}
                                                                    type="button"
                                                                    aria-pressed={!!isLinked}
                                                                    onClick={() => {
                                                                        const currentRepos = draftAgent.repos || [];
                                                                        if (isLinked) {
                                                                            setDraftAgent({ ...draftAgent, repos: currentRepos.filter((id: string) => id !== repo.id) });
                                                                        } else {
                                                                            setDraftAgent({ ...draftAgent, repos: [...currentRepos, repo.id] });
                                                                        }
                                                                    }}
                                                                    className={chip(!!isLinked)}
                                                                >
                                                                    {repo.name} {isLinked && '✓'}
                                                                </button>
                                                            );
                                                        })}
                                                    </div>
                                                    <p className="mt-1 text-xs text-text-faint">Select indexed repositories for semantic code search access.</p>
                                                </div>
                                            )}

                                            {draftAgent.type === 'code' && (
                                                <div className="space-y-1">
                                                    <Label className="block">Linked Databases</Label>
                                                    <div className={boxed}>
                                                        {dbConfigs.length === 0 && <span className="text-sm text-text-muted">No databases configured yet.</span>}
                                                        {dbConfigs.map((db: any) => {
                                                            const isLinked = draftAgent.db_configs?.includes(db.id);
                                                            return (
                                                                <button
                                                                    key={db.id}
                                                                    type="button"
                                                                    aria-pressed={!!isLinked}
                                                                    onClick={() => {
                                                                        const currentDbs = draftAgent.db_configs || [];
                                                                        if (isLinked) {
                                                                            setDraftAgent({ ...draftAgent, db_configs: currentDbs.filter((id: string) => id !== db.id) });
                                                                        } else {
                                                                            setDraftAgent({ ...draftAgent, db_configs: [...currentDbs, db.id] });
                                                                        }
                                                                    }}
                                                                    className={chip(!!isLinked)}
                                                                >
                                                                    {db.name} <span className="opacity-60">{db.db_type}</span> {isLinked && '✓'}
                                                                </button>
                                                            );
                                                        })}
                                                    </div>
                                                    <p className="mt-1 text-xs text-text-faint">Select databases to inject schema context into the agent&apos;s system prompt.</p>
                                                </div>
                                            )}

                                            {/* Tool policy — what the agent may DO with the tools above.
                                                The list is about which tools it holds; this is about what
                                                a call costs. Enforced in core/tool_policy.py. */}
                                            {/* Folded (Naveen, M2d); the closed row still says what is set. */}
                                            <Section
                                                title="Tool Policy"
                                                description="Decided by what a tool does, not by its name — so a destructive call can wait for a person without taking the tool away from the agent."
                                                summary={policySummary(draftAgent.tool_policy || {})}
                                                className="mt-2"
                                            >
                                                <ToolPolicyFields
                                                    value={draftAgent.tool_policy || {}}
                                                    onChange={(next) => setDraftAgent({ ...draftAgent, tool_policy: next })}
                                                    inheritFrom={policyDefaults}
                                                    tools={(draftAgent.tools || []).filter((t: string) => t !== 'all' && !t.startsWith('group:'))}
                                                />
                                            </Section>
                                            {/* Outside the fold: a warning is never folded away. */}
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
                                                    <div className="rounded-md border border-warning/50 bg-warning-subtle p-3">
                                                        <p className="text-xs text-text">
                                                            <span className="font-semibold">{bound.length === 1 ? 'A chat widget publishes' : 'Chat widgets publish'} this agent</span>{' '}
                                                            ({names}). A visitor there is not a member of this workspace and cannot
                                                            answer an approval, so a call to {asked} will end their turn with a note
                                                            asking them to get in touch. Saving is fine — but nobody will be asked.
                                                        </p>
                                                    </div>
                                                );
                                            })()}
                                        </SetupSection>

                                        <SetupSection id="setup-skills" title="Skills and knowledge">
                                            {/* Skills — roster in the prompt, bodies on demand via load_skill */}
                                            <div className="space-y-2">
                                                <Label className="block">Skills</Label>
                                                <div className={boxed}>
                                                    {allSkills.filter((s: any) => !s.shadowed).length === 0 && (
                                                        <span className="text-sm text-text-muted">No skills yet — create one on the Skills page.</span>
                                                    )}
                                                    {allSkills.filter((s: any) => !s.shadowed).map((skill: any) => {
                                                        const isSelected = draftAgent.skills?.includes(skill.name);
                                                        return (
                                                            <button
                                                                key={skill.name}
                                                                type="button"
                                                                aria-pressed={!!isSelected}
                                                                title={skill.description}
                                                                onClick={() => {
                                                                    const current = draftAgent.skills || [];
                                                                    setDraftAgent(isSelected
                                                                        ? { ...draftAgent, skills: current.filter((n: string) => n !== skill.name) }
                                                                        : { ...draftAgent, skills: [...current, skill.name] });
                                                                }}
                                                                className={chip(!!isSelected)}
                                                            >
                                                                {skill.name} {isSelected && '✓'}
                                                            </button>
                                                        );
                                                    })}
                                                </div>
                                                <p className="text-2xs text-text-faint">A selected skill costs one roster line in the prompt; the agent loads its full instructions with load_skill only when needed.</p>
                                            </div>

                                            {/* Knowledge bases — what this agent answers from, with sources (B1b) */}
                                            {knowledgeOn && (
                                                <KnowledgeBasePicker
                                                    variant="switches"
                                                    value={draftAgent.knowledge || []}
                                                    onChange={ids => setDraftAgent({ ...draftAgent, knowledge: ids })}
                                                    bases={allBases}
                                                />
                                            )}
                                        </SetupSection>

                                        {/* Boards (BOARD1b): its own board's switch, and the shared boards that name it; folded, its
                                            fold the section's head (no second title above it). */}
                                        {boardsOn && draftAgent.type !== 'orchestrator' && (
                                            <section id="setup-boards" aria-label="Boards" className="border-b border-border py-6 last:border-b-0">
                                                <OssBoardsHost>
                                                    <AgentBoards
                                                        agentId={isNew ? null : (draftAgent.id || null)}
                                                        own={draftAgent.boards?.own !== false}
                                                        onOwn={own => setDraftAgent({ ...draftAgent, boards: { ...(draftAgent.boards || {}), own } })}
                                                    />
                                                </OssBoardsHost>
                                            </section>
                                        )}

                                        <SetupSection id="setup-limits" title="Limits">
                                            <div className="grid gap-x-6 gap-y-4 @xl:grid-cols-2">
                                                {/* Max Turns */}
                                                <div className="space-y-1">
                                                    <Label htmlFor="age-max-turns" className="block">Max Turns</Label>
                                                    <Input id="age-max-turns"
                                                        type="number"
                                                        min={1}
                                                        max={200}
                                                        value={draftAgent.max_turns ?? (draftAgent.type === 'code' ? 50 : 30)}
                                                        onChange={e => setDraftAgent({ ...draftAgent, max_turns: parseInt(e.target.value) || 30 })}
                                                    />
                                                    <p className="mt-1 text-xs text-text-faint">Max reasoning turns per request. A workflow&apos;s steps override this value.</p>
                                                </div>

                                                {/* Max response length (F263) — this agent's own cap; blank
                                                    leaves it to Settings → Models. `null`, not undefined, when
                                                    cleared: a stored agent reads back `null` (F73). */}
                                                <div className="space-y-1">
                                                    <Label htmlFor="age-max-output" className="block">Max Response Length</Label>
                                                    <Input id="age-max-output"
                                                        type="number"
                                                        inputMode="numeric"
                                                        min={256}
                                                        max={262144}
                                                        step={1024}
                                                        placeholder="Workspace default"
                                                        value={draftAgent.max_output_tokens ?? ''}
                                                        onChange={e => setDraftAgent({
                                                            ...draftAgent,
                                                            max_output_tokens: e.target.value.trim() ? parseInt(e.target.value, 10) : null,
                                                        })}
                                                        aria-describedby="age-max-output-help"
                                                    />
                                                    <p id="age-max-output-help" className="mt-1 text-xs text-text-faint">The most this agent&apos;s model may write in one response, thinking included, in tokens. A model that thinks gets it raised to fit its thinking, so its answer may run past it; the run log says when. Blank uses the workspace&apos;s (Settings → Models).</p>
                                                </div>
                                            </div>

                                            <div className="divide-y divide-border rounded-lg border border-border bg-surface">
                                                {/* Default tools (F414) — the Vault's file tools, a code
                                                    agent's code search and the task list, which the engine
                                                    hands every agent whatever its tools say. Off saves
                                                    `skip_default_tools: true`: the agent gets exactly what
                                                    is ticked. Absent reads as on. */}
                                                <label className="flex cursor-pointer items-start gap-4 p-3">
                                                    <span className="min-w-0 flex-1">
                                                        <span className="block text-sm font-medium text-text">Default tools</span>
                                                        <span id="age-defaults-help" className="mt-0.5 block text-xs text-text-muted">
                                                            Whatever is ticked above, it can always read files (<span className="font-mono">read_file</span>,{' '}
                                                            <span className="font-mono">search_files</span>, <span className="font-mono">list_files</span>…)
                                                            and keep a plan. Turn it off for an agent that only writes what it is asked: it then
                                                            gets exactly the tools ticked, and nothing else.
                                                        </span>
                                                    </span>
                                                    <Switch
                                                        label="Default tools"
                                                        aria-describedby="age-defaults-help"
                                                        checked={draftAgent.skip_default_tools !== true}
                                                        onChange={(on) => setDraftAgent({ ...draftAgent, skip_default_tools: !on })}
                                                        className="mt-0.5"
                                                    />
                                                </label>

                                                {/* Planning — the native task list, on unless this agent
                                                    has no use for one. Above Memory because it is the
                                                    cheaper and more commonly wanted of the two. Off with
                                                    the default tools, which take the task list with them;
                                                    its own value is kept. */}
                                                <label className="flex cursor-pointer items-start gap-4 p-3">
                                                    <span className="min-w-0 flex-1">
                                                        <span id="age-planning-label" className="block text-sm font-medium text-text">Planning</span>
                                                        {/* Named by its heading, described by its sentence (F265). */}
                                                        <span id="age-planning-help" className="mt-0.5 block text-xs text-text-muted">
                                                            Let this agent write a plan with <span className="font-mono">update_tasks</span> and
                                                            keep it up to date as it works. The list shows live in the run log. Turn it off for
                                                            one-shot agents, where announcing three tasks costs a turn and tells nobody anything.
                                                            {draftAgent.skip_default_tools === true && (
                                                                <span className="mt-1 block text-text-faint">Off with the default tools.</span>
                                                            )}
                                                        </span>
                                                    </span>
                                                    <Switch
                                                        label="Planning"
                                                        aria-describedby="age-planning-help"
                                                        checked={draftAgent.planning !== false && draftAgent.skip_default_tools !== true}
                                                        disabled={draftAgent.skip_default_tools === true}
                                                        onChange={(on) => setDraftAgent({ ...draftAgent, planning: on })}
                                                        className="mt-0.5"
                                                    />
                                                </label>

                                                {/* Memory — persistent per-agent memory (index eager, rest on demand) */}
                                                <label className="flex cursor-pointer items-start gap-4 p-3">
                                                    <span className="min-w-0 flex-1">
                                                        <span className="block text-sm font-medium text-text">Memory</span>
                                                        <span id="age-memory-help" className="mt-0.5 block text-xs text-text-muted">
                                                            Give this agent persistent memory: it can save facts with <span className="font-mono">remember</span>,
                                                            search past sessions with <span className="font-mono">search_memory</span>, and its memory index
                                                            rides the prompt. Sessions leave summaries automatically.
                                                        </span>
                                                    </span>
                                                    <Switch
                                                        label="Memory"
                                                        aria-describedby="age-memory-help"
                                                        checked={!!draftAgent.memory}
                                                        onChange={(on) => setDraftAgent({ ...draftAgent, memory: on })}
                                                        className="mt-0.5"
                                                    />
                                                </label>

                                                {/* Helpers (SUB1a, S1): the agent may hand a focused task to a
                                                    throwaway agent of its own. Stored as `spawn_subtask` in its
                                                    tools, as it always was (D204); the grid has no row for it.
                                                    Not for a workflow agent, which runs its workflow. */}
                                                {draftAgent.type !== 'orchestrator' && (
                                                    <div className="p-3">
                                                        <label className="flex cursor-pointer items-start gap-4">
                                                            <span className="min-w-0 flex-1">
                                                                <span className="block text-sm font-medium text-text">Can send helpers</span>
                                                                <span id="age-helpers-help" className="mt-0.5 block text-xs text-text-muted">
                                                                    Let it hand a focused task to a helper with <span className="font-mono">spawn_subtask</span>:
                                                                    a short-lived agent with some of its tools, which asks nobody and sends no helpers of its own.
                                                                    Up to three work at once, each shown in the chat as it works. In the app&apos;s chat a helper
                                                                    can also work in the background and post its result when it is done.
                                                                </span>
                                                            </span>
                                                            <Switch
                                                                label="Can send helpers"
                                                                aria-describedby="age-helpers-help"
                                                                checked={(draftAgent.tools || []).includes(HELPERS_TOOL)}
                                                                onChange={(on) => setDraftAgent({
                                                                    ...draftAgent,
                                                                    tools: on
                                                                        ? [...(draftAgent.tools || []).filter((t: string) => t !== HELPERS_TOOL), HELPERS_TOOL]
                                                                        : (draftAgent.tools || []).filter((t: string) => t !== HELPERS_TOOL),
                                                                })}
                                                                className="mt-0.5"
                                                            />
                                                        </label>
                                                        {(draftAgent.tools || []).includes(HELPERS_TOOL) && (
                                                            <div className="mt-3 space-y-1">
                                                                <Label className="block">Helper model</Label>
                                                                <Combobox
                                                                    value={draftAgent.helper_model || '__same__'}
                                                                    onChange={v => setDraftAgent({ ...draftAgent, helper_model: v === '__same__' ? null : v })}
                                                                    aria-label="Helper model"
                                                                    searchPlaceholder="Search models…"
                                                                    options={helperModelOptions(providers, draftAgent.model || defaultModel || '', quickModel, draftAgent.helper_model)}
                                                                />
                                                                <p className="mt-1 text-xs text-text-faint">What its helpers run on. The quick-step model is set in Settings → Models.</p>
                                                            </div>
                                                        )}
                                                    </div>
                                                )}

                                                {/* Whether a picture reaches it: computed from its saved
                                                    model, not a setting (M2d). A workflow step passes
                                                    images on with its own "Sees attachments". */}
                                                <div className="flex items-start gap-4 p-3">
                                                    <span className="min-w-0 flex-1">
                                                        <span className="block text-sm font-medium text-text">Sees attachments</span>
                                                        <span className="mt-0.5 block text-xs text-text-muted">
                                                            {!savedId
                                                                ? 'Known once the agent is saved.'
                                                                : status?.can_see_images === false
                                                                    ? (status.attach_problem || "This agent's model cannot read images.")
                                                                    : 'People can attach images in chat. Decided by its saved model; a workflow step passes images on with its own setting.'}
                                                        </span>
                                                    </span>
                                                    {savedId && (
                                                        <span className="mt-0.5 inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-text-muted">
                                                            <ImageIcon className="size-3.5" aria-hidden />
                                                            {status?.can_see_images === false ? 'No' : status ? 'Yes' : '—'}
                                                        </span>
                                                    )}
                                                </div>
                                            </div>

                                            {/* A saved agent's memory, whether or not it is on now: what
                                                it remembered before being switched off can still be read
                                                and deleted (F270). */}
                                            {savedId && (
                                                <AgentMemory key={draftAgent.id} agentId={draftAgent.id} />
                                            )}
                                        </SetupSection>

                                        {savedId && (
                                            <SetupSection id="setup-where" title="Where it works">
                                                <WhereItWorks agentId={savedId} channels={agentChannels} agents={agents} widgets={widgets} />
                                            </SetupSection>
                                        )}

                                        {savedId && (
                                            <SetupSection id="setup-month" title="This month">
                                                <ThisMonth key={savedId} agentId={savedId} />
                                            </SetupSection>
                                        )}
                                    </div>
                                )} {/* end agentSubTab === 'config' */}

                                {/* ── Messaging Channels sub-tab ─────────────────── */}
                                {agentSubTab === 'messaging' && (
                                    <div role="tabpanel" aria-label="Messaging Channels" className="space-y-4 pt-5">
                                        <p className="text-xs text-text-faint">
                                            Channels this agent answers on. A channel can front several agents,
                                            and an agent can answer on several channels — assign them from{' '}
                                            <strong className="text-text">Channels</strong>.
                                        </p>
                                        {agentChannels.length === 0 ? (
                                            <div className="space-y-3 rounded-lg border border-dashed border-border p-8 text-center text-text-faint">
                                                <MessageSquare className="mx-auto size-8 opacity-30" aria-hidden />
                                                <p className="text-xs">This agent is not assigned to any channel yet.</p>
                                                <p className="text-xs">Go to <strong className="text-text-muted">Channels</strong>, pick a channel and add this agent to it.</p>
                                            </div>
                                        ) : (
                                            <div className="space-y-2">
                                                {agentChannels.map((ch: any) => {
                                                    const EMOJI: Record<string, string> = { telegram: '✈️', discord: '🎮', slack: '💬', teams: '📘', whatsapp: '📱', twilio_voice: '📞' };
                                                    return (
                                                        <div key={ch.id} className="flex items-center gap-3 rounded-lg border border-border bg-surface p-3">
                                                            <span className="text-lg">{EMOJI[ch.platform] ?? '🤖'}</span>
                                                            <div className="min-w-0 flex-1">
                                                                <div className="text-sm font-medium text-text">{ch.name}</div>
                                                                <div className="text-xs capitalize text-text-faint">
                                                                    {ch.platform === 'twilio_voice' ? `Phone · ${ch.voice?.number ?? ''}` : ch.platform}
                                                                    {ch.agent_ids?.[0] === draftAgent.id ? ' · answers by default' : ''}
                                                                    {(ch.agent_ids?.length ?? 0) > 1 ? ` · ${ch.agent_ids.length} agents` : ''}
                                                                    {ch.multi_agent_mode ? ' · switchable' : ''}
                                                                </div>
                                                            </div>
                                                            {ch.status === 'running'
                                                                ? <span className="flex items-center gap-1 text-xs text-success"><CheckCircle className="size-3.5" aria-hidden /> Running</span>
                                                                : ch.status === 'error'
                                                                    ? <span className="flex items-center gap-1 text-xs text-danger"><XCircle className="size-3.5" aria-hidden /> Error</span>
                                                                    : <span className="flex items-center gap-1 text-xs text-text-faint"><Square className="size-3.5" aria-hidden /> Stopped</span>
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
                                    <div role="tabpanel" aria-label={tabs.find(t => t.id === agentSubTab)?.label} className="pt-5">
                                        <AgentActivity
                                            /* Keyed by agent: switching agents remounts the tab, so its
                                               loading state is its initial state rather than a reset. */
                                            key={draftAgent.id}
                                            agentId={savedId ?? ''}
                                            tab={agentSubTab}
                                            channels={agentChannels}
                                            agents={agents}
                                        />
                                    </div>
                                )}
                            </>)}
                        </div>
                    </div>

                    {/* The Save bar: while there is something to save, on every tab. */}
                    {draftAgent.type !== 'orchestrator' && (dirty || isNew) && (
                        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-t border-border bg-surface px-4 py-3 md:px-8">
                            <span role="status" className="min-w-0 flex-1 text-sm text-text-muted">
                                {isNew ? 'New agent, not saved yet.' : 'Unsaved changes.'}
                                {savedAgent && <span className="hidden text-text-faint sm:inline"> The test on the right uses the saved version.</span>}
                            </span>
                            <Button variant="secondary" size="sm" onClick={onDiscard}>Discard</Button>
                            <Button size="sm" onClick={handleSaveAgent} loading={isSaving} aria-label="Save agent">
                                {isSaving ? 'Saving…' : 'Save'}
                            </Button>
                        </div>
                    )}
                </>) : (
                    <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
                        {loadingAgents ? (
                            <span className="flex items-center gap-2 text-sm text-text-faint"><Spinner size={16} /> Loading agents…</span>
                        ) : (<>
                            <span className="flex size-12 items-center justify-center rounded-2xl bg-machine-subtle text-machine">
                                <Bot className="size-6" aria-hidden />
                            </span>
                            <h2 className="text-base font-semibold text-text">No agents yet</h2>
                            <p className="max-w-sm text-sm text-text-muted">
                                An agent is a model with instructions, and the tools and knowledge it may use. Start one
                                with New agent, or describe it and let Build with AI write it.
                            </p>
                            <Button variant="secondary" size="sm" onClick={() => setAiBuilderOpen(true)}>
                                <Sparkles className="size-4 text-machine" aria-hidden /> Build with AI
                            </Button>
                        </>)}
                    </div>
                )}
            </div>

            {/* ── The test chat ────────────────────────────────────────── */}
            {draftAgent && (testOpen ? (
                test(() => setTestOpen(false), 'hidden min-h-0 w-[380px] shrink-0 flex-col border-l border-border lg:flex')
            ) : (
                <div className="hidden w-11 shrink-0 flex-col items-center border-l border-border py-3 lg:flex">
                    <button
                        type="button"
                        onClick={() => setTestOpen(true)}
                        aria-label={`Try ${savedAgent?.name || 'this agent'}`}
                        title="Show the test chat"
                        className="flex flex-col items-center gap-2 rounded-md px-1.5 py-2 text-text-muted transition-colors hover:bg-row-hover hover:text-text"
                    >
                        <PanelRightOpen className="size-4" aria-hidden />
                        <span className="text-xs font-medium [writing-mode:vertical-rl]">Try {savedAgent?.name || 'it'}</span>
                    </button>
                </div>
            ))}
            {draftAgent && (
                <Drawer open={testDrawer} onClose={() => setTestDrawer(false)} title="Test chat">
                    {test(undefined, '-mx-5 -my-4 flex h-[calc(100%+2rem)] flex-col')}
                </Drawer>
            )}
        </div>
    );
};

/** The tool policy in one line, for its folded row: what is set, or that nothing is. */
/** The agent's tools the grid has no row for, kept when an "every tool" agent's list is spelled out (SUB1a). */
function ownSwitches(tools: string[]): string[] {
    return tools.includes(HELPERS_TOOL) ? [HELPERS_TOOL] : [];
}

/** "Helper model"'s choices: the agent's own (stored as none), the workspace's quick-step model, then every model. */
function helperModelOptions(providers: Record<string, any> | undefined, ownModel: string, quickModel: string,
                            current: string | null | undefined) {
    const models = Object.entries(providers ?? {}).flatMap(([providerKey, info]: [string, any]) =>
        !info.available || info.models.length === 0
            ? []
            : info.models.map((m: string) => ({ value: m, label: m, group: providerLabel(providerKey) })));
    const options = [
        { value: '__same__', label: `Same as this agent (${ownModel || 'not set'})` },
        { value: 'role:quick', label: quickModel ? `Quick-step model · ${quickModel}` : "Quick-step model (none set: runs on this agent's)" },
        ...models,
    ];
    if (current && !options.some(o => o.value === current)) options.push({ value: current, label: `${current} (not available now)` });
    return quickModel || current === 'role:quick' ? options : options.filter(o => o.value !== 'role:quick');
}

function policySummary(policy: ToolPolicy): string {
    const rules = toolRules(policy).length;
    const ruled = rules ? ` · ${rules} rule${rules === 1 ? '' : 's'} for one tool` : '';
    const set = POLICY_CATEGORIES.filter(c => policy[c.id]);
    if (set.length === 0) return `Default for all three${ruled}`;
    const word: Record<string, string> = { allow: 'Allow', confirm: 'Ask first', deny: 'Block' };
    if (set.length > 1) return `${set.length} of 3 set${ruled}`;
    const c = set[0];
    return `${word[policy[c.id]] ?? policy[c.id]}: ${c.label.toLowerCase()}${ruled}`;
}

/** The box a row of chips sits in, with even room above and below whether it holds chips or a note. */
const boxed = 'flex min-h-12 flex-wrap items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2.5';

/** A chip that toggles one item (a repository, a database, a skill). */
const chip = (selected: boolean) => cn(
    'rounded-md border px-2.5 py-1 text-xs font-medium transition-colors',
    selected ? 'border-accent bg-accent-subtle text-text' : 'border-border bg-surface text-text-muted hover:border-border-strong',
);

/** One part of the agent's setup: a heading, and its fields under it. */
function SetupSection({ id, title, children }: { id: string; title: string; children: React.ReactNode }) {
    return (
        <section aria-labelledby={id} className="space-y-4 border-b border-border py-6 last:border-b-0">
            <h3 id={id} className="text-sm font-semibold text-text">{title}</h3>
            {children}
        </section>
    );
}
