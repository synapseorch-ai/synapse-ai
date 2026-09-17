/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';
import { useState, useEffect, useCallback } from 'react';
import { RESOURCE_SAVED_EVENT } from '@/components/app/BuilderProvider';
import {
    Clock, Plus, Trash2, Edit2, Play, RefreshCw,
    ChevronDown, ChevronUp, CheckCircle, AlertCircle,
    ToggleLeft, ToggleRight, Bot, Workflow, Info,
} from 'lucide-react';
import { Select } from '@/components/ui';

// -- Types ----------------------------------------------------------------

interface Schedule {
    id: string;
    name: string;
    description?: string;
    enabled: boolean;
    created_at: string;
    target_type: 'agent' | 'orchestration';
    target_id: string;
    prompt: string;
    schedule_type: 'interval' | 'cron';
    interval_value?: number;
    interval_unit?: 'minutes' | 'hours' | 'days';
    cron_expression?: string;
    missed_run_policy?: 'run_immediately' | 'skip';
    last_run_at?: string | null;
    next_run_at?: string | null;
}

interface Agent { id: string; name: string; }
interface Orchestration { id: string; name: string; }

// -- Cron presets ---------------------------------------------------------

const CRON_PRESETS = [
    { label: 'Every day at...', buildExpr: (h: number) => `0 ${h} * * *` },
    { label: 'Every weekday at...', buildExpr: (h: number) => `0 ${h} * * 1-5` },
    { label: 'Every Monday at...', buildExpr: (h: number) => `0 ${h} * * 1` },
    { label: 'Every Saturday at...', buildExpr: (h: number) => `0 ${h} * * 6` },
    { label: 'Every Sunday at...', buildExpr: (h: number) => `0 ${h} * * 0` },
    { label: '1st of every month at...', buildExpr: (h: number) => `0 ${h} 1 * *` },
    { label: 'Custom expression', buildExpr: () => '' },
];

function describeCron(expr: string): string {
    if (!expr) return '';
    const parts = expr.trim().split(/\s+/);
    if (parts.length !== 5) return expr;
    const [minExpr, hourExpr, domExpr, monExpr, dowExpr] = parts;

    // Time formatting
    const h = parseInt(hourExpr);
    const m = parseInt(minExpr);
    const timeStr = (isNaN(h) || isNaN(m)) 
        ? `${hourExpr}:${minExpr} UTC` 
        : `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')} UTC`;

    const getOrdinal = (n: number) => {
        const s = ["th", "st", "nd", "rd"];
        const v = n % 100;
        const index = (v - 20) % 10;
        return n + (s[index] || s[v] || s[0]);
    };

    const dowMap: Record<string, string> = { 
        '0': 'Sunday', '7': 'Sunday', '1': 'Monday', '2': 'Tuesday', 
        '3': 'Wednesday', '4': 'Thursday', '5': 'Friday', '6': 'Saturday',
        '1-5': 'weekday'
    };

    const monMap: Record<string, string> = {
        '1': 'January', '2': 'February', '3': 'March', '4': 'April',
        '5': 'May', '6': 'June', '7': 'July', '8': 'August',
        '9': 'September', '10': 'October', '11': 'November', '12': 'December'
    };

    const monthName = monMap[monExpr] || '';
    const dayOfWeek = dowMap[dowExpr];
    const dom = parseInt(domExpr);

    // 1. Every day everywhere
    if (domExpr === '*' && monExpr === '*' && dowExpr === '*') {
        return `Every day at ${timeStr}`;
    }

    // 2. Just day of month and month (e.g. 0 17 1 1 * -> 1st of January)
    if (domExpr !== '*' && dowExpr === '*') {
        if (!isNaN(dom)) {
            const ofMonth = monthName ? ` of ${monthName}` : ' of every month';
            return `On the ${getOrdinal(dom)}${ofMonth} at ${timeStr}`;
        }
    }

    // 3. Just day of week and month (e.g. 0 17 * 1 1 -> every Monday in January)
    if (domExpr === '*' && dowExpr !== '*') {
        if (dayOfWeek) {
            const inMonth = monthName ? ` in ${monthName}` : '';
            return `Every ${dayOfWeek}${inMonth} at ${timeStr}`;
        }
    }

    // 4. Month only (e.g. 0 17 * 1 * -> every day in January)
    if (monExpr !== '*' && domExpr === '*' && dowExpr === '*') {
        return `Every day in ${monthName} at ${timeStr}`;
    }

    // 5. Day of month OR Day of week (Standard Cron behavior)
    if (domExpr !== '*' && dowExpr !== '*') {
        if (!isNaN(dom) && dayOfWeek) {
            const ofMonth = monthName ? ` of ${monthName}` : ' of every month';
            const inMonth = monthName ? ` in ${monthName}` : '';
            return `On the ${getOrdinal(dom)}${ofMonth} or every ${dayOfWeek}${inMonth} at ${timeStr}`;
        }
    }

    return expr;
}



function describeSchedule(s: Schedule): string {
    if (s.schedule_type === 'interval') {
        return `Every ${s.interval_value ?? '?'} ${s.interval_unit ?? 'minutes'}`;
    }
    return describeCron(s.cron_expression ?? '');
}

function fmtTime(iso: string | null | undefined): string {
    if (!iso) return '--';
    try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

// -- Empty form -----------------------------------------------------------

function emptyForm(): Omit<Schedule, 'id' | 'created_at' | 'last_run_at' | 'next_run_at'> {
    return {
        name: '',
        description: '',
        enabled: true,
        target_type: 'agent',
        target_id: '',
        prompt: '',
        schedule_type: 'interval',
        interval_value: 30,
        interval_unit: 'minutes',
        cron_expression: '0 9 * * *',
        missed_run_policy: 'skip',
    };
}

// -- Main component -------------------------------------------------------

export const SchedulesTab = () => {
    const [schedules, setSchedules] = useState<Schedule[]>([]);
    const [agents, setAgents] = useState<Agent[]>([]);
    const [orchestrations, setOrchestrations] = useState<Orchestration[]>([]);
    const [loading, setLoading] = useState(false);
    const [showForm, setShowForm] = useState(false);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [form, setForm] = useState(emptyForm());
    const [saving, setSaving] = useState(false);
    const [runningIds, setRunningIds] = useState<Set<string>>(new Set());
    const [toast, setToast] = useState<{ msg: string; ok: boolean } | null>(null);
    const [selectedPresetIdx, setSelectedPresetIdx] = useState(0);
    const [cronHour, setCronHour] = useState(9);
    const [showInstructions, setShowInstructions] = useState(true);

    const showToast = (msg: string, ok = true) => {
        setToast({ msg, ok });
        setTimeout(() => setToast(null), 3000);
    };

    const fetchAll = useCallback(async () => {
        setLoading(true);
        try {
            const [sRes, aRes, oRes] = await Promise.all([
                fetch('/api/schedules'),
                fetch('/api/agents'),
                fetch('/api/orchestrations'),
            ]);
            if (sRes.ok) setSchedules(await sRes.json());
            if (aRes.ok) setAgents(await aRes.json());
            if (oRes.ok) setOrchestrations(await oRes.json());
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        fetchAll();
        // The AI builder reaches this from any screen now, so the
        // list refreshes on the event rather than on a reload.
        window.addEventListener(RESOURCE_SAVED_EVENT, fetchAll);
        return () => window.removeEventListener(RESOURCE_SAVED_EVENT, fetchAll);
    }, [fetchAll]);

    // -- Form helpers ---------------------------------------------------

    function openCreate() {
        setEditingId(null);
        setForm(emptyForm());
        setSelectedPresetIdx(0);
        setCronHour(9);
        setShowInstructions(true);
        setShowForm(true);
    }

    function openEdit(s: Schedule) {
        setEditingId(s.id);
        setForm({
            name: s.name,
            description: s.description ?? '',
            enabled: s.enabled,
            target_type: s.target_type,
            target_id: s.target_id,
            prompt: s.prompt,
            schedule_type: s.schedule_type,
            interval_value: s.interval_value ?? 30,
            interval_unit: s.interval_unit ?? 'minutes',
            cron_expression: s.cron_expression ?? '0 9 * * *',
            missed_run_policy: s.missed_run_policy ?? 'skip',
        });
        // Try to derive cronHour from expression
        if (s.cron_expression) {
            const parts = s.cron_expression.split(' ');
            if (parts.length === 5) {
                const h = parseInt(parts[1]);
                if (!isNaN(h)) setCronHour(h);
            }
        }
        setSelectedPresetIdx(6); // Custom
        setShowInstructions(false);
        setShowForm(true);
    }

    function closeForm() {
        setShowForm(false);
        setEditingId(null);
    }

    function onPresetChange(idx: number) {
        setSelectedPresetIdx(idx);
        if (idx < CRON_PRESETS.length - 1) {
            const expr = CRON_PRESETS[idx].buildExpr(cronHour);
            setForm(f => ({ ...f, cron_expression: expr }));
        }
    }

    function onCronHourChange(h: number) {
        setCronHour(h);
        if (selectedPresetIdx < CRON_PRESETS.length - 1) {
            const expr = CRON_PRESETS[selectedPresetIdx].buildExpr(h);
            setForm(f => ({ ...f, cron_expression: expr }));
        }
    }

    // -- Save -----------------------------------------------------------

    async function saveSchedule() {
        if (!form.name.trim()) { showToast('Name is required', false); return; }
        if (!form.target_id) { showToast('Please select a target agent or orchestration', false); return; }
        if (!form.prompt.trim()) { showToast('Prompt is required', false); return; }
        if (form.schedule_type === 'interval' && (!form.interval_value || form.interval_value < 1)) {
            showToast('Interval value must be at least 1', false); return;
        }
        if (form.schedule_type === 'cron' && !form.cron_expression?.trim()) {
            showToast('Cron expression is required', false); return;
        }

        setSaving(true);
        try {
            const payload: any = { ...form };
            // Clear irrelevant fields
            if (form.schedule_type === 'interval') {
                delete payload.cron_expression;
                delete payload.missed_run_policy;
            } else {
                delete payload.interval_value;
                delete payload.interval_unit;
            }

            const url = editingId ? `/api/schedules/${editingId}` : '/api/schedules';
            const method = editingId ? 'PUT' : 'POST';
            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (res.ok) {
                const saved = await res.json();
                setSchedules(prev => editingId
                    ? prev.map(s => s.id === editingId ? saved : s)
                    : [...prev, saved]
                );
                closeForm();
                showToast(editingId ? 'Schedule updated' : 'Schedule created');
            } else {
                const err = await res.json().catch(() => ({}));
                showToast(err.detail || 'Failed to save', false);
            }
        } finally {
            setSaving(false);
        }
    }

    // -- Toggle enable/disable ------------------------------------------

    async function toggleEnabled(s: Schedule) {
        const res = await fetch(`/api/schedules/${s.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !s.enabled }),
        });
        if (res.ok) {
            const updated = await res.json();
            setSchedules(prev => prev.map(x => x.id === s.id ? updated : x));
        }
    }

    // -- Delete ---------------------------------------------------------

    async function deleteSchedule(id: string) {
        if (!confirm('Delete this schedule?')) return;
        const res = await fetch(`/api/schedules/${id}`, { method: 'DELETE' });
        if (res.ok) {
            setSchedules(prev => prev.filter(s => s.id !== id));
            showToast('Schedule deleted');
        }
    }

    // -- Run now --------------------------------------------------------

    async function runNow(id: string) {
        setRunningIds(prev => new Set(prev).add(id));
        try {
            const res = await fetch(`/api/schedules/${id}/run`, { method: 'POST' });
            if (res.ok) {
                showToast('Schedule triggered successfully');
            } else {
                showToast('Failed to trigger schedule', false);
            }
        } finally {
            setRunningIds(prev => { const n = new Set(prev); n.delete(id); return n; });
        }
    }

    // -- Target label helper --------------------------------------------

    function targetLabel(s: Schedule): string {
        if (s.target_type === 'agent') {
            return agents.find(a => a.id === s.target_id)?.name ?? s.target_id;
        }
        return orchestrations.find(o => o.id === s.target_id)?.name ?? s.target_id;
    }

    // -- Render ---------------------------------------------------------

    return (
        <div className="flex flex-col h-full overflow-hidden font-mono">

            {/* Toast */}
            {toast && (
                <div className={`fixed bottom-6 right-6 z-50 flex items-center gap-2 px-4 py-3 text-sm shadow-lg ${
                    toast.ok ? 'bg-emerald-900/90 text-emerald-200 border border-emerald-700' : 'bg-red-900/90 text-red-200 border border-red-700'
                } rounded-md`}>
                    {toast.ok ? <CheckCircle className="h-4 w-4" /> : <AlertCircle className="h-4 w-4" />}
                    {toast.msg}
                </div>
            )}

            {/* Actions. The title and blurb are the <Screen> frame's. */}
            <div className="px-6 py-3 border-b border-zinc-800 shrink-0 flex items-center justify-end">
                <div className="flex items-center gap-2">
                    <button
                        onClick={fetchAll}
                        className="p-2 text-zinc-500 hover:text-white hover:bg-surface-2 transition-colors"
                        title="Refresh"
                    >
                        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                    </button>
                    <button
                        onClick={openCreate}
                        className="flex items-center gap-2 px-4 py-2 bg-accent text-accent-fg text-sm font-semibold hover:bg-accent-hover transition-colors"
                    >
                        <Plus className="h-4 w-4" />
                        New Schedule
                    </button>
                </div>
            </div>

            {/* Main content area */}
            <div className="flex flex-1 overflow-hidden">

                {/* Schedule list */}
                <div className={`flex flex-col overflow-hidden transition-all ${showForm ? 'w-1/2 border-r border-zinc-800' : 'w-full'}`}>
                    <div className="flex-1 overflow-y-auto">
                        {loading && schedules.length === 0 ? (
                            <div className="flex items-center justify-center h-32 text-zinc-600 text-xs">Loading...</div>
                        ) : schedules.length === 0 ? (
                            <div className="flex flex-col items-center justify-center h-64 gap-4 text-zinc-700 px-8 text-center">
                                <Clock className="h-10 w-10" />
                                <div>
                                    <p className="text-sm font-semibold text-zinc-500">No schedules yet</p>
                                    <p className="text-xs mt-1">Click &quot;New Schedule&quot; to automate your first agent run</p>
                                </div>
                            </div>
                        ) : (
                            <table className="w-full text-xs">
                                <thead>
                                    <tr className="border-b border-zinc-800 text-zinc-500 text-left">
                                        <th className="px-4 py-3 font-medium">Name</th>
                                        <th className="px-4 py-3 font-medium">Target</th>
                                        <th className="px-4 py-3 font-medium">Schedule</th>
                                        <th className="px-4 py-3 font-medium">Next Run</th>
                                        <th className="px-4 py-3 font-medium">Last Run</th>
                                        <th className="px-4 py-3 font-medium text-right">Actions</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {schedules.map(s => (
                                        <tr key={s.id} className="border-b border-zinc-900 hover:bg-row-hover transition-colors group">
                                            <td className="px-4 py-3">
                                                <div className="flex items-center gap-2">
                                                    <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${s.enabled ? 'bg-emerald-400' : 'bg-zinc-600'}`} />
                                                    <span className="font-semibold text-white truncate max-w-[140px]" title={s.name}>{s.name}</span>
                                                </div>
                                                {s.description && (
                                                    <p className="text-zinc-600 truncate max-w-[160px] mt-0.5" title={s.description}>{s.description}</p>
                                                )}
                                            </td>
                                            <td className="px-4 py-3">
                                                <div className="flex items-center gap-1.5 text-zinc-400">
                                                    {s.target_type === 'agent'
                                                        ? <Bot className="h-3 w-3 shrink-0 text-sky-400" />
                                                        : <Workflow className="h-3 w-3 shrink-0 text-accent" />
                                                    }
                                                    <span className="truncate max-w-[100px]" title={targetLabel(s)}>{targetLabel(s)}</span>
                                                </div>
                                            </td>
                                            <td className="px-4 py-3 text-zinc-300">{describeSchedule(s)}</td>
                                            <td className="px-4 py-3 text-zinc-500">{fmtTime(s.next_run_at)}</td>
                                            <td className="px-4 py-3 text-zinc-600">{fmtTime(s.last_run_at)}</td>
                                            <td className="px-4 py-3">
                                                <div className="flex items-center gap-1 justify-end">
                                                    {/* Enable/Disable */}
                                                    <button
                                                        onClick={() => toggleEnabled(s)}
                                                        className="p-1.5 text-zinc-500 hover:text-white transition-colors"
                                                        title={s.enabled ? 'Disable' : 'Enable'}
                                                    >
                                                        {s.enabled
                                                            ? <ToggleRight className="h-4 w-4 text-emerald-400" />
                                                            : <ToggleLeft className="h-4 w-4" />
                                                        }
                                                    </button>
                                                    {/* Run Now */}
                                                    <button
                                                        onClick={() => runNow(s.id)}
                                                        disabled={runningIds.has(s.id)}
                                                        className="p-1.5 text-zinc-500 hover:text-emerald-400 transition-colors disabled:opacity-40"
                                                        title="Run Now"
                                                    >
                                                        <Play className={`h-3.5 w-3.5 ${runningIds.has(s.id) ? 'animate-pulse' : ''}`} />
                                                    </button>
                                                    {/* Edit */}
                                                    <button
                                                        onClick={() => openEdit(s)}
                                                        className="p-1.5 text-zinc-500 hover:text-white transition-colors"
                                                        title="Edit"
                                                    >
                                                        <Edit2 className="h-3.5 w-3.5" />
                                                    </button>
                                                    {/* Delete */}
                                                    <button
                                                        onClick={() => deleteSchedule(s.id)}
                                                        className="p-1.5 text-zinc-500 hover:text-red-400 transition-colors"
                                                        title="Delete"
                                                    >
                                                        <Trash2 className="h-3.5 w-3.5" />
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        )}
                    </div>
                </div>

                {/* Form panel */}
                {showForm && (
                    <div className="w-1/2 flex flex-col overflow-hidden">
                        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-800 shrink-0">
                            <h2 className="text-sm font-semibold text-white">
                                {editingId ? 'Edit Schedule' : 'New Schedule'}
                            </h2>
                            <button onClick={closeForm} className="text-zinc-500 hover:text-white text-xs">? Cancel</button>
                        </div>

                        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-5">

                            {/* Instructions */}
                            <div className="border border-zinc-800 bg-zinc-950 rounded-md">
                                <button
                                    onClick={() => setShowInstructions(v => !v)}
                                    className="w-full flex items-center justify-between px-4 py-3 text-xs text-zinc-400 hover:text-white transition-colors"
                                >
                                    <span className="flex items-center gap-2">
                                        <Info className="h-3.5 w-3.5 text-sky-400" />
                                        <span className="font-semibold">How to create schedules</span>
                                    </span>
                                    {showInstructions ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                                </button>
                                {showInstructions && (
                                    <div className="px-4 pb-4 text-xs text-zinc-500 space-y-3 border-t border-zinc-800">
                                        <div className="pt-3">
                                            <p className="text-zinc-300 font-semibold mb-1">Interval -- run every N minutes/hours/days</p>
                                            <ul className="space-y-1 text-zinc-500">
                                                <li>? <span className="text-zinc-400">Every 5 minutes</span> -- monitor a feed, poll an API</li>
                                                <li>? <span className="text-zinc-400">Every 2 hours</span> -- periodic summaries or reports</li>
                                                <li>? <span className="text-zinc-400">Every 1 day</span> -- daily digest or cleanup tasks</li>
                                            </ul>
                                            <p className="text-zinc-600 mt-1.5">Interval restarts from the last run time. If the server was offline and the schedule is overdue, it runs immediately on restart.</p>
                                        </div>
                                        <div>
                                            <p className="text-zinc-300 font-semibold mb-1">Cron / Fixed Time -- run at specific times</p>
                                            <ul className="space-y-1 text-zinc-500">
                                                <li>? <span className="text-zinc-400">Every day at 9 AM</span> -- morning standup report</li>
                                                <li>? <span className="text-zinc-400">Every Monday at 6 PM</span> -- weekly digest</li>
                                                <li>? <span className="text-zinc-400">1st of month</span> -- monthly invoice summary</li>
                                            </ul>
                                            <p className="text-zinc-600 mt-1.5">If a run was missed while offline, choose whether to run immediately or skip to the next scheduled time.</p>
                                        </div>
                                        <div>
                                            <p className="text-zinc-300 font-semibold mb-1">Prompt</p>
                                            <p className="text-zinc-500">The prompt is what the agent will receive each time the schedule fires. Think of it as a standing instruction.</p>
                                        </div>
                                        <div>
                                            <p className="text-zinc-300 font-semibold mb-1">Messaging notifications</p>
                                            <p className="text-zinc-500">If the selected agent has a connected messaging channel (Slack, Telegram, etc.), the result is sent there automatically after each run.</p>
                                        </div>
                                    </div>
                                )}
                            </div>

                            {/* Name */}
                            <div>
                                <label htmlFor="sched-name" className="block text-xs text-zinc-400 mb-1.5 font-semibold">Name <span className="text-red-400">*</span></label>
                                <input
                                    id="sched-name"
                                    value={form.name}
                                    onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                                    placeholder="e.g. Daily Sales Report"
                                    className="w-full bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500 rounded-md"
                                />
                            </div>

                            {/* Description */}
                            <div>
                                <label htmlFor="sched-description" className="block text-xs text-zinc-400 mb-1.5 font-semibold">Description</label>
                                <input
                                    id="sched-description"
                                    value={form.description}
                                    onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
                                    placeholder="Optional short description"
                                    className="w-full bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500 rounded-md"
                                />
                            </div>

                            {/* Target type */}
                            <div>
                                <label className="block text-xs text-zinc-400 mb-1.5 font-semibold">Run as</label>
                                <div className="flex gap-2">
                                    {(['agent', 'orchestration'] as const).map(t => (
                                        <button
                                            key={t}
                                            onClick={() => setForm(f => ({ ...f, target_type: t, target_id: '' }))}
                                            className={`flex items-center gap-2 px-4 py-2 text-xs font-medium border transition-all ${
                                                form.target_type === t
                                                    ? 'border-white bg-accent text-accent-fg'
                                                    : 'border-zinc-700 text-zinc-400 hover:text-white hover:border-zinc-500'
                                            } rounded-md`}
                                        >
                                            {t === 'agent' ? <Bot className="h-3.5 w-3.5" /> : <Workflow className="h-3.5 w-3.5" />}
                                            {t === 'agent' ? 'Agent' : 'Orchestration'}
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Target selector */}
                            <div>
                                <label className="block text-xs text-zinc-400 mb-1.5 font-semibold">
                                    {form.target_type === 'agent' ? 'Agent' : 'Orchestration'} <span className="text-red-400">*</span>
                                </label>
                                <Select
                                    value={form.target_id || undefined}
                                    onChange={v => setForm(f => ({ ...f, target_id: v }))}
                                    placeholder={`Select ${form.target_type === 'agent' ? 'an agent' : 'an orchestration'}…`}
                                    aria-label="Target"
                                    options={(form.target_type === 'agent' ? agents : orchestrations)
                                        .map(item => ({ value: item.id, label: item.name }))}
                                />
                            </div>

                            {/* Prompt */}
                            <div>
                                <label htmlFor="sched-prompt" className="block text-xs text-zinc-400 mb-1.5 font-semibold">
                                    Prompt <span className="text-red-400">*</span>
                                    <span className="ml-2 font-normal text-zinc-600">What should the agent do each time it runs?</span>
                                </label>
                                <textarea
                                    id="sched-prompt"
                                    value={form.prompt}
                                    onChange={e => setForm(f => ({ ...f, prompt: e.target.value }))}
                                    placeholder="e.g. Generate a daily summary of yesterday's sales data and highlight any anomalies."
                                    rows={4}
                                    className="w-full bg-zinc-900 border border-zinc-700 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500 resize-none rounded-md"
                                />
                            </div>

                            {/* Schedule type */}
                            <div>
                                <label className="block text-xs text-zinc-400 mb-1.5 font-semibold">Schedule Type</label>
                                <div className="flex gap-2">
                                    {(['interval', 'cron'] as const).map(t => (
                                        <button
                                            key={t}
                                            onClick={() => setForm(f => ({ ...f, schedule_type: t }))}
                                            className={`flex items-center gap-2 px-4 py-2 text-xs font-medium border transition-all ${
                                                form.schedule_type === t
                                                    ? 'border-white bg-accent text-accent-fg'
                                                    : 'border-zinc-700 text-zinc-400 hover:text-white hover:border-zinc-500'
                                            } rounded-md`}
                                        >
                                            <Clock className="h-3.5 w-3.5" />
                                            {t === 'interval' ? 'Interval' : 'Fixed Time (Cron)'}
                                        </button>
                                    ))}
                                </div>
                            </div>

                            {/* Interval config */}
                            {form.schedule_type === 'interval' && (
                                <div className="bg-zinc-900/60 border border-zinc-800 p-4 space-y-3 rounded-md">
                                    <div className="flex items-center gap-3">
                                        <span className="text-xs text-zinc-400 shrink-0">Run every</span>
                                        <input
                                            aria-label="Run every"
                                            type="number"
                                            min={1}
                                            value={form.interval_value ?? 30}
                                            onChange={e => setForm(f => ({ ...f, interval_value: parseInt(e.target.value) || 1 }))}
                                            className="w-20 bg-zinc-800 border border-zinc-700 px-2 py-1.5 text-sm text-white text-center focus:outline-none focus:border-zinc-500 rounded-md"
                                        />
                                        <Select
                                            value={form.interval_unit ?? 'minutes'}
                                            onChange={v => setForm(f => ({ ...f, interval_unit: v as any }))}
                                            aria-label="Interval unit"
                                            className="w-32"
                                            options={[
                                                { value: 'minutes', label: 'minutes' },
                                                { value: 'hours', label: 'hours' },
                                                { value: 'days', label: 'days' },
                                            ]}
                                        />
                                    </div>
                                    <p className="text-xs text-zinc-600">
                                        Runs every {form.interval_value ?? '?'} {form.interval_unit ?? 'minutes'} from the last run.
                                        If overdue after a restart, it runs immediately.
                                    </p>
                                </div>
                            )}

                            {/* Cron config */}
                            {form.schedule_type === 'cron' && (
                                <div className="bg-zinc-900/60 border border-zinc-800 p-4 space-y-4 rounded-md">
                                    {/* Preset picker */}
                                    <div>
                                        <label className="block text-xs text-zinc-500 mb-1.5">Quick preset</label>
                                        <Select
                                            value={String(selectedPresetIdx)}
                                            onChange={v => onPresetChange(parseInt(v, 10))}
                                            aria-label="Schedule preset"
                                            options={CRON_PRESETS.map((p, i) => ({ value: String(i), label: p.label }))}
                                        />
                                    </div>

                                    {/* Hour picker (when preset has a time component) */}
                                    {selectedPresetIdx < CRON_PRESETS.length - 1 && (
                                        <div className="flex items-center gap-3">
                                            <span className="text-xs text-zinc-400">At hour (UTC)</span>
                                            <Select
                                                value={String(cronHour)}
                                                onChange={v => onCronHourChange(parseInt(v, 10))}
                                                aria-label="Hour (UTC)"
                                                className="w-36"
                                                options={Array.from({ length: 24 }, (_, i) => ({
                                                    value: String(i),
                                                    label: `${i.toString().padStart(2, '0')}:00 UTC`,
                                                }))}
                                            />
                                        </div>
                                    )}

                                    {/* Expression input */}
                                    <div>
                                        <label htmlFor="sched-cron" className="block text-xs text-zinc-500 mb-1.5">
                                            Cron expression
                                            <span className="ml-2 text-zinc-700">minute hour day-of-month month day-of-week</span>
                                        </label>
                                        <input
                                            id="sched-cron"
                                            value={form.cron_expression ?? ''}
                                            onChange={e => setForm(f => ({ ...f, cron_expression: e.target.value }))}
                                            placeholder="0 9 * * *"
                                            className="w-full bg-zinc-800 border border-zinc-700 px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-zinc-500 rounded-md"
                                        />
                                        {form.cron_expression && (
                                            <p className="text-xs text-emerald-400 mt-1">{describeCron(form.cron_expression)}</p>
                                        )}
                                    </div>

                                    {/* Missed run policy */}
                                    <div>
                                        <label className="block text-xs text-zinc-500 mb-2">If a run was missed while offline</label>
                                        <div className="space-y-2">
                                            {[
                                                { value: 'run_immediately', label: 'Run immediately when server starts', desc: 'Catches up the missed run once, then resumes normal schedule' },
                                                { value: 'skip', label: 'Skip and wait for the next scheduled time', desc: 'Ignores the missed run, next run fires at the correct future time' },
                                            ].map(opt => (
                                                <label key={opt.value} className="flex items-start gap-3 cursor-pointer group">
                                                    <input
                                                        type="radio"
                                                        name="missed_run_policy"
                                                        value={opt.value}
                                                        checked={form.missed_run_policy === opt.value}
                                                        onChange={() => setForm(f => ({ ...f, missed_run_policy: opt.value as any }))}
                                                        className="mt-0.5 shrink-0"
                                                    />
                                                    <div>
                                                        <p className="text-xs text-zinc-300">{opt.label}</p>
                                                        <p className="text-[11px] text-zinc-600">{opt.desc}</p>
                                                    </div>
                                                </label>
                                            ))}
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* Enabled toggle */}
                            <div className="flex items-center gap-3">
                                <span className="text-xs text-zinc-400 font-semibold">Enabled</span>
                                <button
                                    role="switch"
                                    aria-checked={form.enabled}
                                    aria-label="Enabled"
                                    onClick={() => setForm(f => ({ ...f, enabled: !f.enabled }))}
                                    className="text-zinc-400 hover:text-white transition-colors"
                                >
                                    {form.enabled
                                        ? <ToggleRight className="h-5 w-5 text-emerald-400" />
                                        : <ToggleLeft className="h-5 w-5" />
                                    }
                                </button>
                                <span className="text-xs text-zinc-600">{form.enabled ? 'Schedule will run automatically' : 'Schedule is paused'}</span>
                            </div>

                            {/* Save button */}
                            <div className="pt-2 pb-4">
                                <button
                                    onClick={saveSchedule}
                                    disabled={saving}
                                    className="w-full py-2.5 bg-accent text-accent-fg text-sm font-semibold hover:bg-accent-hover disabled:opacity-50 transition-colors"
                                >
                                    {saving ? 'Saving...' : editingId ? 'Save Changes' : 'Create Schedule'}
                                </button>
                            </div>

                        </div>
                    </div>
                )}
            </div>
        </div>
    );
};
