'use client';
import { useState } from 'react';
import { Trash2, AlertCircle, CheckCircle2, MessageSquare, BarChart2, GitBranch, Server, Activity, Bot, Brain } from 'lucide-react';
import { ConfirmationModal } from './ConfirmationModal';
import { Button, Checkbox, Section } from '@/components/ui';
import { FORM } from './form';

type ClearItem = 'chat_history' | 'orchestration_history' | 'agent_logs' | 'agent_memory' | 'usage' | 'repos' | 'db_configs';

const ITEMS: { id: ClearItem; icon: React.ElementType; label: string; description: string; detail: string }[] = [
    {
        id: 'chat_history',
        icon: MessageSquare,
        label: 'Chat history',
        description: 'All saved conversations across all sessions',
        detail: 'All conversation messages and session state files',
    },
    {
        id: 'orchestration_history',
        icon: Activity,
        label: 'Orchestration run history',
        description: 'Past orchestration run logs and checkpoints',
        detail: 'All orchestration execution logs and run state files',
    },
    {
        id: 'agent_logs',
        icon: Bot,
        label: 'Agent logs',
        description: 'Per-run debug logs for individual agent executions',
        detail: 'All agent run log files from chat and orchestration sources',
    },
    {
        // What agents with memory saved (F270): it could not be removed at all
        // before, short of asking each agent to forget.
        id: 'agent_memory',
        icon: Brain,
        label: 'Agent memory',
        description: 'Everything agents with memory have remembered',
        detail: "Every agent's memory index, saved memories and session traces",
    },
    {
        id: 'usage',
        icon: BarChart2,
        label: 'Usage history',
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
        label: 'Database connections',
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
        <div>
            <Section variant="plain" collapsible={false} title="Data categories" description="Select the data pipelines you want to permanently clear.">
                {/* A real checkbox inside a label: Tab reaches it, Space ticks it
                    and a screen reader hears its state; the whole row still
                    toggles on a click (F219). Rows, not cards (UX1). */}
                <ul className={`m-0 grid ${FORM} list-none grid-cols-1 gap-x-8 p-0 md:grid-cols-2`}>
                    {ITEMS.map((item) => {
                        const Icon = item.icon;
                        const isChecked = selected[item.id];
                        return (
                            <li key={item.id} className="border-b border-border">
                                <label className="group flex cursor-pointer items-start gap-3 rounded-md px-1 py-3 transition-colors hover:bg-row-hover">
                                    <Checkbox
                                        checked={isChecked}
                                        onChange={() => toggle(item.id)}
                                        label={item.label}
                                        className="mt-0.5"
                                    />
                                    <span className="block min-w-0 flex-1">
                                        <span className="flex items-center gap-2">
                                            <Icon className="size-4 shrink-0 text-text-faint" aria-hidden />
                                            <span className="text-sm font-medium text-text">{item.label}</span>
                                        </span>
                                        <span className="mt-1 block text-xs leading-relaxed text-text-muted">{item.description}</span>
                                    </span>
                                </label>
                            </li>
                        );
                    })}
                </ul>

                {/* Result message */}
                {result && (
                    <p role="status" className={`mt-4 flex items-center gap-2 text-sm ${result.type === 'success' ? 'text-text' : 'text-danger'}`}>
                        {result.type === 'success'
                            ? <CheckCircle2 className="size-4 shrink-0 text-success" aria-hidden />
                            : <AlertCircle className="size-4 shrink-0" aria-hidden />}
                        <span>{result.text}</span>
                    </p>
                )}
            </Section>

            {/* Danger zone: the danger is the button's, not the box's (UX1). */}
            <Section variant="plain" collapsible={false} title="Danger zone">
                <div className={`flex ${FORM} flex-col justify-between gap-4 md:flex-row md:items-center`}>
                    <p className="flex items-start gap-2 text-sm leading-relaxed text-text-muted">
                        <Trash2 className="mt-0.5 size-4 shrink-0 text-text-faint" aria-hidden />
                        Actions here are irreversible. Selecting items and clearing them will permanently wipe their corresponding data.
                    </p>
                    <Button
                        variant="danger"
                        className="shrink-0"
                        disabled={selectedCount === 0 || clearing}
                        onClick={() => setShowConfirm(true)}
                    >
                        {clearing ? 'Clearing...' : selectedCount === 0 ? 'Clear selected' : `Clear selected (${selectedCount})`}
                    </Button>
                </div>
            </Section>

            <ConfirmationModal
                isOpen={showConfirm}
                title="Confirm data deletion"
                message={confirmMessage}
                confirmText="Yes, clear selected"
                onConfirm={handleClear}
                onClose={() => setShowConfirm(false)}
            />
        </div>
    );
};
