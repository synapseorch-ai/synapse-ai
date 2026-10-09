'use client';

/*
 * Workflows › Schedules (M2h): what starts on its own, how often, when it runs
 * next and how its last run went, as the shared `components/run/ScheduleList`
 * rows cloud draws too. A schedule opens in a drawer with every field it had in
 * the half-page form (Naveen, M2h's plan), and how often it runs is said in the
 * same words as the Workflows list's chips (`scheduleWords` / `cronWords`; the
 * page's own `describeCron` is retired).
 *
 * The last run's status is known for a workflow's schedule: its runs carry the
 * `schedule_<id>` session (Run now's too), so the runs list (one read of the
 * newest 200) says how the latest went. An agent's schedule shows only when.
 */
import { useCallback, useEffect, useState } from 'react';
import { Bot, Edit2, Play, Plus, RefreshCw, Trash2, Workflow } from 'lucide-react';

import { RESOURCE_SAVED_EVENT } from '@/components/app/BuilderProvider';
import { AppScreen } from '@/components/app/AppScreen';
import { cronWords, lastScheduledRun, ScheduleList, scheduleRow } from '@/components/run';
import type { RunSummary } from '@/components/runs/summary';
import {
    Button, buttonStyles, ConfirmDialog, Drawer, EmptyState, Field, IconButton, Input, Section, Segmented, Select, Switch,
    Textarea, Toast, useToast,
} from '@/components/ui';
import { cn } from '@/lib/cn';

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

type Form = Omit<Schedule, 'id' | 'created_at' | 'last_run_at' | 'next_run_at'>;

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

const RUNS_READ = 200;

// -- Empty form -----------------------------------------------------------

function emptyForm(): Form {
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
    const [runs, setRuns] = useState<RunSummary[]>([]);
    const [loading, setLoading] = useState(false);
    const [loaded, setLoaded] = useState(false);
    const [showForm, setShowForm] = useState(false);
    const [editingId, setEditingId] = useState<string | null>(null);
    const [form, setForm] = useState<Form>(emptyForm());
    const [saving, setSaving] = useState(false);
    const [runningIds, setRunningIds] = useState<Set<string>>(new Set());
    const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
    const [selectedPresetIdx, setSelectedPresetIdx] = useState(0);
    const [cronHour, setCronHour] = useState(9);
    const { toast, show, dismiss } = useToast();

    const showToast = useCallback((msg: string, ok = true) => show(msg, ok ? 'success' : 'danger'), [show]);

    const fetchAll = useCallback(async () => {
        setLoading(true);
        try {
            const [sRes, aRes, oRes, rRes] = await Promise.all([
                fetch('/api/schedules'),
                fetch('/api/agents'),
                fetch('/api/orchestrations'),
                fetch(`/api/orchestrations/runs?limit=${RUNS_READ}`).catch(() => null),
            ]);
            if (sRes.ok) setSchedules(await sRes.json());
            if (aRes.ok) setAgents(await aRes.json());
            if (oRes.ok) setOrchestrations(await oRes.json());
            if (rRes?.ok) {
                const r = await rRes.json();
                if (Array.isArray(r)) setRuns(r);
            }
        } finally {
            setLoading(false);
            setLoaded(true);
        }
    }, []);

    useEffect(() => {
        void fetchAll();
        // The AI builder reaches this from any screen now, so the
        // list refreshes on the event rather than on a reload.
        const refresh = () => void fetchAll();
        window.addEventListener(RESOURCE_SAVED_EVENT, refresh);
        return () => window.removeEventListener(RESOURCE_SAVED_EVENT, refresh);
    }, [fetchAll]);

    // -- Form helpers ---------------------------------------------------

    function openCreate() {
        setEditingId(null);
        setForm(emptyForm());
        setSelectedPresetIdx(0);
        setCronHour(9);
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
        if (!form.target_id) { showToast('Please select a target agent or workflow', false); return; }
        if (!form.prompt.trim()) { showToast('Prompt is required', false); return; }
        if (form.schedule_type === 'interval' && (!form.interval_value || form.interval_value < 1)) {
            showToast('Interval value must be at least 1', false); return;
        }
        if (form.schedule_type === 'cron' && !form.cron_expression?.trim()) {
            showToast('Cron expression is required', false); return;
        }

        setSaving(true);
        try {
            const payload: Partial<Form> = { ...form };
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
                // A 422's detail is a list of problems: say the first one's words.
                const detail = Array.isArray(err.detail) ? err.detail[0]?.msg : err.detail;
                showToast(typeof detail === 'string' && detail ? detail : 'Failed to save', false);
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
        setConfirmDelete(null);
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

    const rows = schedules.map(s => {
        const run = s.target_type === 'orchestration' ? lastScheduledRun(s.id, runs) : undefined;
        return scheduleRow(s, targetLabel(s), run ? { status: run.status, waitingForHuman: run.waiting_for_human, runId: run.run_id } : null);
    });
    const byId = new Map(schedules.map(s => [s.id, s]));
    const deleting = confirmDelete ? byId.get(confirmDelete) : undefined;
    const presetIsCustom = selectedPresetIdx === CRON_PRESETS.length - 1;
    const label = 'block text-sm font-medium text-text';

    const actions = (
        <>
            <button
                type="button"
                aria-label="Refresh"
                title="Refresh"
                onClick={() => void fetchAll()}
                className={buttonStyles({ variant: 'ghost', size: 'md', iconOnly: true })}
            >
                <RefreshCw aria-hidden className={cn('size-4', loading && 'animate-spin')} />
            </button>
            <Button onClick={openCreate}>
                <Plus className="h-4 w-4" aria-hidden />
                New schedule
            </Button>
        </>
    );

    return (
        <AppScreen id="schedules" width="wide" actions={actions}>
            {!loaded && loading ? (
                <p className="py-10 text-center text-sm text-text-faint">Loading...</p>
            ) : (
                <ScheduleList
                    rows={rows}
                    onOpen={(row) => { const s = byId.get(row.id); if (s) openEdit(s); }}
                    runHref={(runId) => `/runs/${encodeURIComponent(runId)}`}
                    actionsWidth="152px"
                    actions={(row) => {
                        const s = byId.get(row.id);
                        if (!s) return null;
                        return (
                            <>
                                <span className="mr-1 flex items-center" title={s.enabled ? 'Disable' : 'Enable'}>
                                    <Switch
                                        size="sm"
                                        checked={s.enabled}
                                        onChange={() => void toggleEnabled(s)}
                                        label={`${s.name} enabled`}
                                    />
                                </span>
                                <IconButton
                                    label={`Run ${s.name} now`}
                                    title="Run now"
                                    icon={Play}
                                    size="sm"
                                    disabled={runningIds.has(s.id)}
                                    className={runningIds.has(s.id) ? 'motion-safe:animate-pulse' : undefined}
                                    onClick={() => void runNow(s.id)}
                                />
                                <IconButton label={`Edit ${s.name}`} title="Edit" icon={Edit2} size="sm" onClick={() => openEdit(s)} />
                                <IconButton
                                    label={`Delete ${s.name}`}
                                    title="Delete"
                                    icon={Trash2}
                                    size="sm"
                                    className="text-text-faint hover:bg-danger-subtle hover:text-danger"
                                    onClick={() => setConfirmDelete(s.id)}
                                />
                            </>
                        );
                    }}
                    empty={
                        <EmptyState title="No schedules yet">
                            Click “New schedule” to automate your first agent run.
                        </EmptyState>
                    }
                />
            )}

            <Drawer
                open={showForm}
                onClose={closeForm}
                title={editingId ? 'Edit schedule' : 'New schedule'}
                width="lg"
                footer={
                    <>
                        <Button variant="ghost" onClick={closeForm}>Cancel</Button>
                        <Button onClick={() => void saveSchedule()} disabled={saving}>
                            {saving ? 'Saving...' : editingId ? 'Save changes' : 'Create schedule'}
                        </Button>
                    </>
                }
            >
                <div className="grid gap-5">
                    {/* Open for a new schedule, folded when editing one (as before). */}
                    <Section
                        key={editingId ?? 'new'}
                        variant="plain"
                        title="How to create schedules"
                        defaultOpen={!editingId}
                    >
                        <div className="grid gap-3 text-[13px] text-text-muted">
                            <div>
                                <p className="m-0 font-medium text-text">Interval — run every N minutes/hours/days</p>
                                <ul className="m-0 mt-1 list-disc space-y-0.5 pl-5">
                                    <li><span className="text-text">Every 5 minutes</span> — monitor a feed, poll an API</li>
                                    <li><span className="text-text">Every 2 hours</span> — periodic summaries or reports</li>
                                    <li><span className="text-text">Every 1 day</span> — daily digest or cleanup tasks</li>
                                </ul>
                                <p className="m-0 mt-1.5 text-text-faint">Interval restarts from the last run time. If the server was offline and the schedule is overdue, it runs immediately on restart.</p>
                            </div>
                            <div>
                                <p className="m-0 font-medium text-text">Cron / Fixed time — run at specific times</p>
                                <ul className="m-0 mt-1 list-disc space-y-0.5 pl-5">
                                    <li><span className="text-text">Every day at 9 AM</span> — morning standup report</li>
                                    <li><span className="text-text">Every Monday at 6 PM</span> — weekly digest</li>
                                    <li><span className="text-text">1st of month</span> — monthly invoice summary</li>
                                </ul>
                                <p className="m-0 mt-1.5 text-text-faint">If a run was missed while offline, choose whether to run immediately or skip to the next scheduled time.</p>
                            </div>
                            <div>
                                <p className="m-0 font-medium text-text">Prompt</p>
                                <p className="m-0 text-text-faint">The prompt is what the agent will receive each time the schedule fires. Think of it as a standing instruction.</p>
                            </div>
                            <div>
                                <p className="m-0 font-medium text-text">Messaging notifications</p>
                                <p className="m-0 text-text-faint">If the selected agent has a connected messaging channel (Slack, Telegram, etc.), the result is sent there automatically after each run.</p>
                            </div>
                        </div>
                    </Section>

                    <Field label="Name" required>
                        <Input
                            id="sched-name"
                            value={form.name}
                            onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                            placeholder="e.g. Daily Sales Report"
                        />
                    </Field>

                    <Field label="Description">
                        <Input
                            id="sched-description"
                            value={form.description}
                            onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
                            placeholder="Optional short description"
                        />
                    </Field>

                    <div className="grid gap-1.5">
                        <span className={label}>Run as</span>
                        <Segmented
                            label="Run as"
                            value={form.target_type}
                            onChange={t => setForm(f => ({ ...f, target_type: t, target_id: '' }))}
                            options={[
                                { value: 'agent', label: 'Agent', icon: <Bot className="h-3.5 w-3.5" aria-hidden /> },
                                { value: 'orchestration', label: 'Workflow', icon: <Workflow className="h-3.5 w-3.5" aria-hidden /> },
                            ]}
                        />
                    </div>

                    <Field label={form.target_type === 'agent' ? 'Agent' : 'Workflow'} required>
                        <Select
                            value={form.target_id || undefined}
                            onChange={v => setForm(f => ({ ...f, target_id: v }))}
                            placeholder={`Select ${form.target_type === 'agent' ? 'an agent' : 'a workflow'}…`}
                            aria-label="Target"
                            options={(form.target_type === 'agent' ? agents : orchestrations)
                                .map(item => ({ value: item.id, label: item.name }))}
                        />
                    </Field>

                    <Field label="Prompt" required hint="What should the agent do each time it runs?">
                        <Textarea
                            id="sched-prompt"
                            value={form.prompt}
                            onChange={e => setForm(f => ({ ...f, prompt: e.target.value }))}
                            placeholder="e.g. Generate a daily summary of yesterday's sales data and highlight any anomalies."
                            rows={4}
                        />
                    </Field>

                    <div className="grid gap-1.5">
                        <span className={label}>Schedule type</span>
                        <Segmented
                            label="Schedule type"
                            value={form.schedule_type}
                            onChange={t => setForm(f => ({ ...f, schedule_type: t }))}
                            options={[
                                { value: 'interval', label: 'Interval' },
                                { value: 'cron', label: 'Fixed time (cron)' },
                            ]}
                        />
                    </div>

                    {form.schedule_type === 'interval' && (
                        <div className="grid gap-2 rounded-md bg-surface-2 p-4">
                            <div className="flex items-center gap-3">
                                <span className="shrink-0 text-[13px] text-text-muted">Run every</span>
                                <Input
                                    aria-label="Run every"
                                    type="number"
                                    min={1}
                                    value={form.interval_value ?? 30}
                                    onChange={e => setForm(f => ({ ...f, interval_value: parseInt(e.target.value) || 1 }))}
                                    className="w-20 text-center"
                                />
                                <Select
                                    value={form.interval_unit ?? 'minutes'}
                                    onChange={v => setForm(f => ({ ...f, interval_unit: v as Form['interval_unit'] }))}
                                    aria-label="Interval unit"
                                    className="w-32"
                                    options={[
                                        { value: 'minutes', label: 'minutes' },
                                        { value: 'hours', label: 'hours' },
                                        { value: 'days', label: 'days' },
                                    ]}
                                />
                            </div>
                            <p className="m-0 text-xs text-text-faint">
                                Runs every {form.interval_value ?? '?'} {form.interval_unit ?? 'minutes'} from the last run.
                                If overdue after a restart, it runs immediately.
                            </p>
                        </div>
                    )}

                    {form.schedule_type === 'cron' && (
                        <div className="grid gap-4 rounded-md bg-surface-2 p-4">
                            <Field label="Quick preset">
                                <Select
                                    value={String(selectedPresetIdx)}
                                    onChange={v => onPresetChange(parseInt(v, 10))}
                                    aria-label="Schedule preset"
                                    options={CRON_PRESETS.map((p, i) => ({ value: String(i), label: p.label }))}
                                />
                            </Field>

                            {/* The hour, when the preset has one. */}
                            {!presetIsCustom && (
                                <div className="flex items-center gap-3">
                                    <span className="text-[13px] text-text-muted">At hour (UTC)</span>
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

                            <Field label="Cron expression" hint="minute hour day-of-month month day-of-week">
                                <Input
                                    id="sched-cron"
                                    value={form.cron_expression ?? ''}
                                    onChange={e => setForm(f => ({ ...f, cron_expression: e.target.value }))}
                                    placeholder="0 9 * * *"
                                    className="font-code"
                                />
                            </Field>
                            {form.cron_expression && (
                                <p data-cron-words className="-mt-2 m-0 text-[13px] font-medium text-text">{cronWords(form.cron_expression)}</p>
                            )}

                            <fieldset className="m-0 grid gap-2 border-0 p-0">
                                <legend className="mb-2 text-sm font-medium text-text">If a run was missed while offline</legend>
                                {[
                                    { value: 'run_immediately', label: 'Run immediately when server starts', desc: 'Catches up the missed run once, then resumes normal schedule' },
                                    { value: 'skip', label: 'Skip and wait for the next scheduled time', desc: 'Ignores the missed run, next run fires at the correct future time' },
                                ].map(opt => (
                                    <label key={opt.value} className="flex cursor-pointer items-start gap-3">
                                        <input
                                            type="radio"
                                            name="missed_run_policy"
                                            value={opt.value}
                                            checked={form.missed_run_policy === opt.value}
                                            onChange={() => setForm(f => ({ ...f, missed_run_policy: opt.value as Form['missed_run_policy'] }))}
                                            className="mt-1 shrink-0 accent-[var(--color-accent)]"
                                        />
                                        <span>
                                            <span className="block text-[13px] text-text">{opt.label}</span>
                                            <span className="block text-xs text-text-faint">{opt.desc}</span>
                                        </span>
                                    </label>
                                ))}
                            </fieldset>
                        </div>
                    )}

                    <div className="flex items-center gap-3">
                        <span className={label} id="sched-enabled">Enabled</span>
                        <Switch
                            checked={form.enabled}
                            onChange={(on) => setForm(f => ({ ...f, enabled: on }))}
                            label="Enabled"
                        />
                        <span className="text-xs text-text-faint">{form.enabled ? 'Schedule will run automatically' : 'Schedule is paused'}</span>
                    </div>
                </div>
            </Drawer>

            <ConfirmDialog
                open={confirmDelete !== null}
                onClose={() => setConfirmDelete(null)}
                title="Delete this schedule?"
                description={deleting ? `“${deleting.name}” stops running. Runs it already started keep their history.` : undefined}
                confirmLabel="Delete"
                onConfirm={() => confirmDelete && void deleteSchedule(confirmDelete)}
            />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </AppScreen>
    );
};
