'use client';
import { useState } from 'react';
import { Trash2, AlertCircle, CheckCircle2, MessageSquare, BarChart2, GitBranch, Server, Activity, Bot, Brain } from 'lucide-react';
import { ConfirmationModal } from './ConfirmationModal';
import { Checkbox } from '@/components/ui';

type ClearItem = 'chat_history' | 'orchestration_history' | 'agent_logs' | 'agent_memory' | 'usage' | 'repos' | 'db_configs';

const ITEMS: { id: ClearItem; icon: React.ElementType; label: string; description: string; detail: string }[] = [
    {
        id: 'chat_history',
        icon: MessageSquare,
        label: 'Chat History',
        description: 'All saved conversations across all sessions',
        detail: 'All conversation messages and session state files',
    },
    {
        id: 'orchestration_history',
        icon: Activity,
        label: 'Orchestration Run History',
        description: 'Past orchestration run logs and checkpoints',
        detail: 'All orchestration execution logs and run state files',
    },
    {
        id: 'agent_logs',
        icon: Bot,
        label: 'Agent Logs',
        description: 'Per-run debug logs for individual agent executions',
        detail: 'All agent run log files from chat and orchestration sources',
    },
    {
        // What agents with memory saved (F270): it could not be removed at all
        // before, short of asking each agent to forget.
        id: 'agent_memory',
        icon: Brain,
        label: 'Agent Memory',
        description: 'Everything agents with memory have remembered',
        detail: "Every agent's memory index, saved memories and session traces",
    },
    {
        id: 'usage',
        icon: BarChart2,
        label: 'Usage History',
        description: 'Token usage and cost tracking records',
        detail: 'All token/cost logs across all sessions',
    },
    {
        id: 'repos',
        icon: GitBranch,
        label: 'Repositories',
        description: 'All indexed code repositories and their vector indexes',
        detail: 'All repo configurations and code search indexes',
    },
    {
        id: 'db_configs',
        icon: Server,
        label: 'Database Connections',
        description: 'All saved database configurations',
        detail: 'All DB connection strings and configuration data',
    },
];

const DEFAULT_SELECTED: Record<ClearItem, boolean> = {
    chat_history: false,
    orchestration_history: false,
    agent_logs: false,
    agent_memory: false,
    usage: false,
    repos: false,
    db_configs: false,
};

export const MemoryTab = () => {
    const [selected, setSelected] = useState<Record<ClearItem, boolean>>(DEFAULT_SELECTED);
    const [showConfirm, setShowConfirm] = useState(false);
    const [clearing, setClearing] = useState(false);
    const [result, setResult] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

    const selectedItems = ITEMS.filter(i => selected[i.id]);
    const selectedCount = selectedItems.length;

    const toggle = (id: ClearItem) => {
        setSelected(prev => ({ ...prev, [id]: !prev[id] }));
        setResult(null);
    };

    const handleClear = async () => {
        setClearing(true);
        setResult(null);
        try {
            const items = (Object.keys(selected) as ClearItem[]).filter(k => selected[k]);
            const res = await fetch('/api/memory/clear', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items }),
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error((err as { detail?: string }).detail || 'Failed to clear data');
            }
            const labels = selectedItems.map(i => i.label).join(', ');
            setResult({ type: 'success', text: `Successfully cleared: ${labels}` });
            setSelected(DEFAULT_SELECTED);
        } catch (e: unknown) {
            setResult({ type: 'error', text: e instanceof Error ? e.message : 'An error occurred' });
        } finally {
            setClearing(false);
        }
    };

    const confirmMessage =
        `The following data will be permanently deleted:\n\n` +
        selectedItems.map(i => `• ${i.label}: ${i.detail}`).join('\n') +
        `\n\nThis action cannot be undone.`;

    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between border-b border-zinc-800/50 pb-4">
                <div>
                    <h3 className="text-lg font-bold text-zinc-100 flex items-center gap-2">
                        Data Categories
                    </h3>
                    <p className="text-text-faint text-sm mt-1">Select the data pipelines you want to permanently clear.</p>
                </div>
            </div>

            {/* Checkbox list */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {ITEMS.map((item) => {
                    const Icon = item.icon;
                    const isChecked = selected[item.id];
                    // A real checkbox inside a label: Tab reaches it, Space ticks it
                    // and a screen reader hears its state; the whole card still
                    // toggles on a click (F219).
                    return (
                        <label
                            key={item.id}
                            className={`flex items-start gap-4 p-4 border transition-all cursor-pointer group ${
                                isChecked
                                    ? 'bg-red-950/20 border-red-900/50'
                                    : 'bg-zinc-900/40 border-zinc-800/50 hover:border-zinc-700/50 hover:bg-zinc-900/80'
                            } rounded-md`}
                        >
                            <Checkbox
                                checked={isChecked}
                                onChange={() => toggle(item.id)}
                                label={item.label}
                                className="mt-0.5 bg-zinc-950 border-zinc-700 group-hover:border-zinc-500 data-[state=checked]:bg-red-500 data-[state=checked]:border-red-500"
                            />
                            <span className="block flex-1 min-w-0">
                                <span className="flex items-center gap-2 mb-1">
                                    <Icon className={`h-4 w-4 flex-shrink-0 transition-colors ${isChecked ? 'text-red-400' : 'text-text-faint group-hover:text-zinc-400'}`} />
                                    <span className={`text-sm font-bold tracking-wide transition-colors ${isChecked ? 'text-red-200' : 'text-zinc-200 group-hover:text-zinc-100'}`}>{item.label}</span>
                                </span>
                                <span className={`block text-xs transition-colors mt-1.5 leading-relaxed ${isChecked ? 'text-red-400':'text-text-faint group-hover:text-zinc-400'}`}>{item.description}</span>
                            </span>
                        </label>
                    );
                })}
            </div>

            {/* Result message */}
            {result && (
                <div className={`flex items-center gap-2 text-sm p-3 border ${result.type === 'success' ? 'text-green-400 border-green-900/50 bg-green-950/10' : 'text-red-400 border-red-900/50 bg-red-950/10'} rounded-md`}>
                    {result.type === 'success'
                        ? <CheckCircle2 className="h-4 w-4 flex-shrink-0" />
                        : <AlertCircle className="h-4 w-4 flex-shrink-0" />}
                    <span>{result.text}</span>
                </div>
            )}

            {/* Danger Zone */}
            <div className="mt-8 pt-6 border-t border-zinc-800/50">
                <div className="bg-transparent border border-red-900/30 p-5 md:p-6 flex flex-col md:flex-row md:items-center justify-between gap-4 md:gap-8 hover:border-red-900/50 transition-colors rounded-md">
                    <div>
                        <h3 className="text-sm uppercase tracking-wider font-bold text-red-500 flex items-center gap-2">
                            <Trash2 className="h-4 w-4" /> Danger Zone
                        </h3>
                        <p className="text-xs text-red-400 mt-1.5 leading-relaxed">
                            Actions here are irreversible. Selecting items and clearing them will permanently wipe their corresponding data.
                        </p>
                    </div>
                    <button
                        disabled={selectedCount === 0 || clearing}
                        onClick={() => setShowConfirm(true)}
                        className="flex-shrink-0 px-6 py-2.5 text-sm font-bold bg-red-950/40 border border-red-900/50 text-red-500 hover:bg-red-900/60 hover:text-red-400 hover:border-red-500/80 disabled:opacity-40 disabled:cursor-not-allowed transition-all rounded-md"
                    >
                        {clearing ? 'Clearing...' : selectedCount === 0 ? 'Clear Selected' : `Clear Selected (${selectedCount})`}
                    </button>
                </div>
            </div>

            <ConfirmationModal
                isOpen={showConfirm}
                title="Confirm Data Deletion"
                message={confirmMessage}
                confirmText="Yes, Clear Selected"
                onConfirm={handleClear}
                onClose={() => setShowConfirm(false)}
            />
        </div>
    );
};
