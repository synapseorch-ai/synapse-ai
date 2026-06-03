"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import { Loader2, CheckCircle2, XCircle, RefreshCw, Trash2, Plus, AlertTriangle, Activity, Server, Database, Zap, Users, Eye } from 'lucide-react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ScaleConfig {
    redis_url: string;
    scale_postgres_url: string;
    scale_mode_enabled: boolean;
    scale_auto_sync: boolean;
    worker_concurrency: number;
    otlp_endpoint: string;
    metrics_token: string;
    max_global_queue_depth: number;
    rate_limit_per_tenant_rps: number;
    pgbouncer_mode: boolean;
    num_queue_shards: number;
}

interface SyncStatus {
    available: boolean;
    counts?: Record<string, number>;
}

interface WorkerInfo {
    worker_id: string;
    hostname: string;
    address: string;
    status: 'online' | 'offline' | 'draining';
    active_jobs: number;
    max_jobs: number;
    last_heartbeat: string | null;
    mcp_disabled: string[];
}

interface QueueStats {
    available: boolean;
    queued: number;
    active: number;
    failed: number;
    dlq_count: number;
}

interface DLQEntry {
    id: string;
    run_id: string;
    orchestration_id: string;
    job_function: string;
    error_message: string;
    attempt_count: number;
    last_failed_at: string;
}

interface Tenant {
    tenant_id: string;
    name: string;
    max_concurrent_runs: number;
    max_queued_runs: number;
    priority: number;
}

const DEFAULT_CONFIG: ScaleConfig = {
    redis_url: '',
    scale_postgres_url: '',
    scale_mode_enabled: false,
    scale_auto_sync: false,
    worker_concurrency: 10,
    otlp_endpoint: '',
    metrics_token: '',
    max_global_queue_depth: 1000000,
    rate_limit_per_tenant_rps: 1000,
    pgbouncer_mode: false,
    num_queue_shards: 1,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
    const colors: Record<string, string> = {
        online: 'text-emerald-400 bg-emerald-400/10',
        offline: 'text-red-400 bg-red-400/10',
        draining: 'text-amber-400 bg-amber-400/10',
        ok: 'text-emerald-400 bg-emerald-400/10',
        error: 'text-red-400 bg-red-400/10',
    };
    return (
        <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${colors[status] || 'text-zinc-400 bg-zinc-800'}`}>
            <span className="w-1.5 h-1.5 rounded-full bg-current" />
            {status}
        </span>
    );
}

function SectionHeader({ icon: Icon, title, subtitle }: { icon: any; title: string; subtitle: string }) {
    return (
        <div className="flex items-start gap-3 mb-6">
            <div className="p-2 bg-zinc-900 border border-zinc-800">
                <Icon className="h-4 w-4 text-zinc-400" />
            </div>
            <div>
                <h3 className="text-sm font-bold text-zinc-100 tracking-wider uppercase">{title}</h3>
                <p className="text-xs text-zinc-500 mt-0.5">{subtitle}</p>
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ScaleTab() {
    const [config, setConfig] = useState<ScaleConfig>(DEFAULT_CONFIG);
    const [isSaving, setIsSaving] = useState(false);
    const [saveMsg, setSaveMsg] = useState('');

    // Connection test state
    const [redisTesting, setRedisTesting] = useState(false);
    const [redisStatus, setRedisStatus] = useState<{ ok: boolean; msg: string } | null>(null);
    const [pgTesting, setPgTesting] = useState(false);
    const [pgStatus, setPgStatus] = useState<{ ok: boolean; msg: string } | null>(null);

    // Sync state
    const [syncing, setSyncing] = useState(false);
    const [syncResult, setSyncResult] = useState<any>(null);
    const [syncStatus, setSyncStatus] = useState<SyncStatus | null>(null);

    // Workers
    const [workers, setWorkers] = useState<WorkerInfo[]>([]);
    const [workerHealthResults, setWorkerHealthResults] = useState<Record<string, any>>({});
    const [newWorkerAddress, setNewWorkerAddress] = useState('');

    // Queue stats
    const [queueStats, setQueueStats] = useState<QueueStats | null>(null);

    // DLQ
    const [dlqEntries, setDlqEntries] = useState<DLQEntry[]>([]);
    const [dlqLoading, setDlqLoading] = useState(false);
    const [retryingDlq, setRetryingDlq] = useState<string | null>(null);
    const [expandedDlq, setExpandedDlq] = useState<string | null>(null);

    // Tenants
    const [tenants, setTenants] = useState<Tenant[]>([]);
    const [newTenant, setNewTenant] = useState({ tenant_id: '', name: '', max_concurrent_runs: 100, max_queued_runs: 1000 });
    const [addingTenant, setAddingTenant] = useState(false);

    // Load config on mount
    useEffect(() => {
        fetch('/api/scale/config')
            .then(r => r.json())
            .then(data => setConfig({ ...DEFAULT_CONFIG, ...data }))
            .catch(() => {});

        fetch('/api/scale/sync/status')
            .then(r => r.json())
            .then(setSyncStatus)
            .catch(() => {});

        loadWorkers();
        loadQueueStats();
        loadDlq();
        loadTenants();
    }, []);

    // Poll queue stats every 5s when scale mode is on
    useEffect(() => {
        if (!config.scale_mode_enabled) return;
        const interval = setInterval(loadQueueStats, 5000);
        return () => clearInterval(interval);
    }, [config.scale_mode_enabled]);

    const loadWorkers = useCallback(() => {
        fetch('/api/scale/workers')
            .then(r => r.json())
            .then(data => setWorkers(Array.isArray(data) ? data : []))
            .catch(() => setWorkers([]));
    }, []);

    const loadQueueStats = useCallback(() => {
        fetch('/api/scale/queue')
            .then(r => r.json())
            .then(data => data && typeof data === 'object' ? setQueueStats(data) : null)
            .catch(() => {});
    }, []);

    const loadDlq = useCallback(() => {
        setDlqLoading(true);
        fetch('/api/scale/dlq')
            .then(r => r.json())
            .then(data => setDlqEntries(Array.isArray(data) ? data : []))
            .catch(() => setDlqEntries([]))
            .finally(() => setDlqLoading(false));
    }, []);

    const loadTenants = useCallback(() => {
        fetch('/api/scale/tenants')
            .then(r => r.json())
            .then(data => setTenants(Array.isArray(data) ? data : []))
            .catch(() => setTenants([]));
    }, []);

    const handleSave = async () => {
        setIsSaving(true);
        setSaveMsg('');
        try {
            await fetch('/api/scale/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(config),
            });
            setSaveMsg('Saved. Restart the server to apply Redis/Postgres changes.');
        } catch {
            setSaveMsg('Save failed.');
        } finally {
            setIsSaving(false);
        }
    };

    const testRedis = async () => {
        setRedisTesting(true);
        setRedisStatus(null);
        try {
            const r = await fetch('/api/scale/test-redis', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ redis_url: config.redis_url }),
            });
            const d = await r.json();
            setRedisStatus({ ok: d.status === 'ok', msg: d.message });
        } catch (e) {
            setRedisStatus({ ok: false, msg: String(e) });
        } finally {
            setRedisTesting(false);
        }
    };

    const testPostgres = async () => {
        setPgTesting(true);
        setPgStatus(null);
        try {
            const r = await fetch('/api/scale/test-postgres', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ postgres_url: config.scale_postgres_url }),
            });
            const d = await r.json();
            setPgStatus({ ok: d.status === 'ok', msg: d.message });
        } catch (e) {
            setPgStatus({ ok: false, msg: String(e) });
        } finally {
            setPgTesting(false);
        }
    };

    const triggerSync = async () => {
        setSyncing(true);
        setSyncResult(null);
        try {
            const r = await fetch('/api/scale/sync', { method: 'POST' });
            const d = await r.json();
            setSyncResult(d);
            const status = await fetch('/api/scale/sync/status').then(r => r.json());
            setSyncStatus(status);
        } catch (e) {
            setSyncResult({ error: String(e) });
        } finally {
            setSyncing(false);
        }
    };

    const checkWorkerHealth = async (worker: WorkerInfo) => {
        try {
            const r = await fetch(`/api/scale/workers/${worker.worker_id}/health`);
            const d = await r.json();
            setWorkerHealthResults(prev => ({ ...prev, [worker.worker_id]: d }));
        } catch (e) {
            setWorkerHealthResults(prev => ({ ...prev, [worker.worker_id]: { status: 'error', message: String(e) } }));
        }
    };

    const removeWorker = async (workerId: string) => {
        await fetch(`/api/scale/workers/${workerId}`, { method: 'DELETE' });
        loadWorkers();
    };

    const retryDlq = async (dlqId: string) => {
        setRetryingDlq(dlqId);
        try {
            await fetch(`/api/scale/dlq/${dlqId}/retry`, { method: 'POST' });
            loadDlq();
        } finally {
            setRetryingDlq(null);
        }
    };

    const addTenant = async () => {
        if (!newTenant.tenant_id) return;
        setAddingTenant(true);
        try {
            await fetch('/api/scale/tenants', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newTenant),
            });
            setNewTenant({ tenant_id: '', name: '', max_concurrent_runs: 100, max_queued_runs: 1000 });
            loadTenants();
        } finally {
            setAddingTenant(false);
        }
    };

    const deleteTenant = async (tenantId: string) => {
        await fetch(`/api/scale/tenants/${tenantId}`, { method: 'DELETE' });
        loadTenants();
    };

    const field = (key: keyof ScaleConfig) => ({
        value: String(config[key] ?? ''),
        onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
            setConfig(prev => ({ ...prev, [key]: e.target.type === 'number' ? Number(e.target.value) : e.target.value })),
    });

    const toggle = (key: keyof ScaleConfig) => ({
        checked: Boolean(config[key]),
        onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
            setConfig(prev => ({ ...prev, [key]: e.target.checked })),
    });

    // ---------------------------------------------------------------------------
    // Render
    // ---------------------------------------------------------------------------

    return (
        <div className="space-y-8">

            {/* ── Section 1: Redis Connection ─────────────────────────────── */}
            <div className="border border-zinc-800 p-6">
                <SectionHeader
                    icon={Zap}
                    title="Redis Connection"
                    subtitle="Required for distributed workers, queue management, and distributed cancellation."
                />

                <div className="space-y-4">
                    <div>
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">Redis URL</label>
                        <div className="flex gap-2">
                            <input
                                type="text"
                                placeholder="redis://localhost:6379/0"
                                className="flex-1 bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 font-mono focus:outline-none focus:border-zinc-500"
                                {...field('redis_url')}
                            />
                            <button
                                onClick={testRedis}
                                disabled={redisTesting || !config.redis_url}
                                className="px-4 py-2 text-xs font-bold uppercase tracking-wider bg-zinc-800 text-zinc-300 hover:bg-zinc-700 disabled:opacity-40 transition-colors flex items-center gap-2"
                            >
                                {redisTesting ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                                Test
                            </button>
                        </div>
                        {redisStatus && (
                            <p className={`text-xs mt-1.5 flex items-center gap-1.5 ${redisStatus.ok ? 'text-emerald-400' : 'text-red-400'}`}>
                                {redisStatus.ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                                {redisStatus.msg}
                            </p>
                        )}
                        <p className="text-[10px] text-zinc-600 mt-1.5">For Redis Cluster: <span className="font-mono">redis+cluster://host1:6379,host2:6379</span></p>
                    </div>

                    <div className="flex items-center justify-between py-3 border-t border-zinc-800">
                        <div>
                            <p className="text-sm text-zinc-300 font-medium">Enable Scale Mode</p>
                            <p className="text-xs text-zinc-500 mt-0.5">Route V2 API calls through Redis ARQ workers instead of in-process execution.</p>
                        </div>
                        <label className="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" className="sr-only peer" {...toggle('scale_mode_enabled')} />
                            <div className="w-11 h-6 bg-zinc-700 rounded-full peer peer-checked:bg-zinc-100 transition-colors" />
                            <div className="absolute left-1 top-1 bg-white w-4 h-4 rounded-full transition-transform peer-checked:translate-x-5" />
                        </label>
                    </div>

                    <div className="grid grid-cols-2 gap-4 pt-2">
                        <div>
                            <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">Queue Shards</label>
                            <input type="number" min="1" max="64" className="w-full bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-zinc-500" {...field('num_queue_shards')} />
                            <p className="text-[10px] text-zinc-600 mt-1">Use &gt;1 for Redis Cluster shard-aware routing.</p>
                        </div>
                        <div>
                            <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">Worker Concurrency</label>
                            <input type="number" min="1" max="100" className="w-full bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-zinc-500" {...field('worker_concurrency')} />
                            <p className="text-[10px] text-zinc-600 mt-1">Max parallel jobs per worker process.</p>
                        </div>
                    </div>
                </div>
            </div>

            {/* ── Section 2: Postgres Sync ────────────────────────────────── */}
            <div className="border border-zinc-800 p-6">
                <SectionHeader
                    icon={Database}
                    title="Postgres Sync"
                    subtitle="Workers read orchestrations, agents, and tools from Postgres. Sync after any changes."
                />

                <div className="space-y-4">
                    <div>
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">Postgres URL (Scale)</label>
                        <div className="flex gap-2">
                            <input
                                type="text"
                                placeholder="postgresql://user:pass@host:5432/synapse"
                                className="flex-1 bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 font-mono focus:outline-none focus:border-zinc-500"
                                {...field('scale_postgres_url')}
                            />
                            <button
                                onClick={testPostgres}
                                disabled={pgTesting || !config.scale_postgres_url}
                                className="px-4 py-2 text-xs font-bold uppercase tracking-wider bg-zinc-800 text-zinc-300 hover:bg-zinc-700 disabled:opacity-40 transition-colors flex items-center gap-2"
                            >
                                {pgTesting ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                                Test
                            </button>
                        </div>
                        {pgStatus && (
                            <p className={`text-xs mt-1.5 flex items-center gap-1.5 ${pgStatus.ok ? 'text-emerald-400' : 'text-red-400'}`}>
                                {pgStatus.ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                                {pgStatus.msg}
                            </p>
                        )}
                        <p className="text-[10px] text-zinc-600 mt-1.5">Separate from the embed-code Postgres. PgBouncer: enable the toggle below and point to PgBouncer port.</p>
                    </div>

                    <div className="flex items-center justify-between py-3 border-t border-zinc-800">
                        <div>
                            <p className="text-sm text-zinc-300 font-medium">PgBouncer Mode</p>
                            <p className="text-xs text-zinc-500 mt-0.5">Use NullPool (required when Postgres URL points to PgBouncer in transaction mode).</p>
                        </div>
                        <label className="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" className="sr-only peer" {...toggle('pgbouncer_mode')} />
                            <div className="w-11 h-6 bg-zinc-700 rounded-full peer peer-checked:bg-zinc-100 transition-colors" />
                            <div className="absolute left-1 top-1 bg-white w-4 h-4 rounded-full transition-transform peer-checked:translate-x-5" />
                        </label>
                    </div>

                    <div className="flex items-center justify-between py-3 border-t border-zinc-800">
                        <div>
                            <p className="text-sm text-zinc-300 font-medium">Auto-sync on change</p>
                            <p className="text-xs text-zinc-500 mt-0.5">Automatically sync to Postgres whenever you save an orchestration, agent, or tool.</p>
                        </div>
                        <label className="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" className="sr-only peer" {...toggle('scale_auto_sync')} />
                            <div className="w-11 h-6 bg-zinc-700 rounded-full peer peer-checked:bg-zinc-100 transition-colors" />
                            <div className="absolute left-1 top-1 bg-white w-4 h-4 rounded-full transition-transform peer-checked:translate-x-5" />
                        </label>
                    </div>

                    <div className="flex items-center gap-4 pt-2">
                        <button
                            onClick={triggerSync}
                            disabled={syncing || !config.scale_postgres_url}
                            className="flex items-center gap-2 px-4 py-2 text-xs font-bold uppercase tracking-wider bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-40 transition-colors"
                        >
                            {syncing ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                            Sync Now
                        </button>
                        {syncStatus?.available && syncStatus.counts && (
                            <div className="flex gap-4 text-[10px] text-zinc-500 font-mono">
                                {Object.entries(syncStatus.counts).map(([k, v]) => (
                                    <span key={k}>{k}: <span className="text-zinc-300">{v}</span></span>
                                ))}
                            </div>
                        )}
                    </div>

                    {syncResult && (
                        <div className={`mt-3 p-3 border text-xs font-mono ${syncResult.errors?.length ? 'border-red-800 text-red-400' : 'border-emerald-800 text-emerald-400'}`}>
                            {syncResult.errors?.length
                                ? `${syncResult.total_synced} synced, ${syncResult.errors.length} error(s): ${syncResult.errors[0]}`
                                : `✓ Synced ${syncResult.total_synced} items at ${syncResult.synced_at}`
                            }
                        </div>
                    )}

                    <div className="flex items-start gap-2 p-3 bg-amber-900/20 border border-amber-800/40 text-amber-400">
                        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
                        <p className="text-xs">Workers read definitions from Postgres only. Always sync before running V2 jobs after making changes.</p>
                    </div>
                </div>
            </div>

            {/* ── Section 3: Workers ──────────────────────────────────────── */}
            <div className="border border-zinc-800 p-6">
                <SectionHeader
                    icon={Server}
                    title="Workers"
                    subtitle="Remote worker instances running Dockerfile.worker. Each worker pulls jobs from Redis ARQ."
                />

                {workers.length === 0 ? (
                    <div className="text-center py-8 border border-zinc-800 border-dashed">
                        <Server className="h-8 w-8 text-zinc-700 mx-auto mb-3" />
                        <p className="text-sm text-zinc-500">No workers registered.</p>
                        <p className="text-xs text-zinc-600 mt-1">Deploy <span className="font-mono text-zinc-400">Dockerfile.worker</span> and register its health endpoint below.</p>
                    </div>
                ) : (
                    <div className="space-y-2 mb-4">
                        {workers.map(w => {
                            const health = workerHealthResults[w.worker_id];
                            return (
                                <div key={w.worker_id} className="border border-zinc-800 p-4">
                                    <div className="flex items-start justify-between gap-4">
                                        <div className="flex-1 min-w-0">
                                            <div className="flex items-center gap-2 flex-wrap">
                                                <span className="text-xs font-mono text-zinc-300">{w.worker_id}</span>
                                                <StatusBadge status={w.status} />
                                                <span className="text-[10px] text-zinc-500">{w.active_jobs}/{w.max_jobs} jobs</span>
                                            </div>
                                            <p className="text-xs text-zinc-500 mt-1 font-mono">{w.address}</p>
                                            {w.mcp_disabled?.length > 0 && (
                                                <p className="text-[10px] text-amber-500 mt-1">MCP disabled: {w.mcp_disabled.join(', ')}</p>
                                            )}
                                            {w.last_heartbeat && (
                                                <p className="text-[10px] text-zinc-600 mt-0.5">Last heartbeat: {new Date(w.last_heartbeat).toLocaleTimeString()}</p>
                                            )}
                                            {health && (
                                                <p className={`text-[10px] mt-1 ${health.status === 'ok' ? 'text-emerald-400' : 'text-red-400'}`}>
                                                    Health: {health.status} {health.latency_ms != null ? `(${health.latency_ms}ms)` : ''}
                                                    {health.data?.mcp_disabled?.length > 0 ? ` — MCP disabled: ${health.data.mcp_disabled.join(', ')}` : ''}
                                                </p>
                                            )}
                                        </div>
                                        <div className="flex gap-2 shrink-0">
                                            <button
                                                onClick={() => checkWorkerHealth(w)}
                                                className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider border border-zinc-700 text-zinc-400 hover:text-zinc-100 hover:border-zinc-500 transition-colors flex items-center gap-1"
                                            >
                                                <Activity className="h-3 w-3" /> Health
                                            </button>
                                            <button
                                                onClick={() => removeWorker(w.worker_id)}
                                                className="p-1.5 text-zinc-600 hover:text-red-400 transition-colors"
                                            >
                                                <Trash2 className="h-3.5 w-3.5" />
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                )}

                <div className="flex gap-2 mt-4 pt-4 border-t border-zinc-800">
                    <input
                        type="text"
                        placeholder="http://10.0.0.5:9000"
                        className="flex-1 bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 font-mono focus:outline-none focus:border-zinc-500"
                        value={newWorkerAddress}
                        onChange={e => setNewWorkerAddress(e.target.value)}
                    />
                    <button
                        disabled={!newWorkerAddress}
                        onClick={() => {
                            fetch('/api/scale/workers', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ address: newWorkerAddress }),
                            }).then(() => { setNewWorkerAddress(''); loadWorkers(); });
                        }}
                        className="flex items-center gap-2 px-4 py-2 text-xs font-bold uppercase tracking-wider border border-zinc-700 text-zinc-300 hover:text-zinc-100 hover:border-zinc-500 disabled:opacity-40 transition-colors"
                    >
                        <Plus className="h-3 w-3" /> Register
                    </button>
                    <button onClick={loadWorkers} className="p-2 text-zinc-500 hover:text-zinc-300 transition-colors">
                        <RefreshCw className="h-4 w-4" />
                    </button>
                </div>
            </div>

            {/* ── Section 4: Queue & DLQ ──────────────────────────────────── */}
            <div className="border border-zinc-800 p-6">
                <SectionHeader
                    icon={Activity}
                    title="Queue & Dead Letter Queue"
                    subtitle="Live queue depth and failed jobs that exhausted all retries."
                />

                {/* Queue Stats Cards */}
                <div className="grid grid-cols-3 gap-4 mb-6">
                    {[
                        { label: 'Queued', value: queueStats?.queued ?? '—', color: 'text-zinc-100' },
                        { label: 'Active', value: queueStats?.active ?? '—', color: 'text-emerald-400' },
                        { label: 'DLQ', value: queueStats?.dlq_count ?? '—', color: 'text-red-400' },
                    ].map(stat => (
                        <div key={stat.label} className="border border-zinc-800 p-4 text-center">
                            <p className={`text-2xl font-bold font-mono ${stat.color}`}>{stat.value}</p>
                            <p className="text-[10px] uppercase tracking-wider text-zinc-500 mt-1">{stat.label}</p>
                        </div>
                    ))}
                </div>

                {/* DLQ entries */}
                {dlqLoading ? (
                    <div className="flex items-center gap-2 text-zinc-500 text-sm">
                        <Loader2 className="h-4 w-4 animate-spin" /> Loading failed jobs...
                    </div>
                ) : dlqEntries.length === 0 ? (
                    <div className="text-center py-6 border border-zinc-800 border-dashed">
                        <CheckCircle2 className="h-6 w-6 text-zinc-700 mx-auto mb-2" />
                        <p className="text-sm text-zinc-500">No failed jobs.</p>
                    </div>
                ) : (
                    <div className="space-y-2">
                        {dlqEntries.map(entry => (
                            <div key={entry.id} className="border border-zinc-800">
                                <div className="flex items-center justify-between gap-4 p-3">
                                    <div className="flex-1 min-w-0">
                                        <div className="flex items-center gap-2 flex-wrap">
                                            <span className="text-xs font-mono text-zinc-300">{entry.run_id || entry.id}</span>
                                            <span className="text-[10px] text-zinc-600 font-mono">{entry.job_function}</span>
                                            <span className="text-[10px] text-red-400">{entry.attempt_count} attempts</span>
                                        </div>
                                        <p className="text-[11px] text-red-400 mt-1 truncate">{entry.error_message}</p>
                                    </div>
                                    <div className="flex gap-2 shrink-0">
                                        <button
                                            onClick={() => setExpandedDlq(expandedDlq === entry.id ? null : entry.id)}
                                            className="p-1.5 text-zinc-600 hover:text-zinc-300 transition-colors"
                                        >
                                            <Eye className="h-3.5 w-3.5" />
                                        </button>
                                        <button
                                            onClick={() => retryDlq(entry.id)}
                                            disabled={retryingDlq === entry.id}
                                            className="px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider border border-zinc-700 text-zinc-400 hover:text-zinc-100 disabled:opacity-40 flex items-center gap-1 transition-colors"
                                        >
                                            {retryingDlq === entry.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                                            Retry
                                        </button>
                                    </div>
                                </div>
                                {expandedDlq === entry.id && (
                                    <div className="p-3 border-t border-zinc-800 bg-zinc-950">
                                        <pre className="text-[10px] text-zinc-500 font-mono whitespace-pre-wrap overflow-x-auto">{entry.error_message}</pre>
                                    </div>
                                )}
                            </div>
                        ))}
                        <button onClick={loadDlq} className="text-xs text-zinc-500 hover:text-zinc-300 flex items-center gap-1 mt-2">
                            <RefreshCw className="h-3 w-3" /> Refresh
                        </button>
                    </div>
                )}
            </div>

            {/* ── Section 5: Tenants ──────────────────────────────────────── */}
            <div className="border border-zinc-800 p-6">
                <SectionHeader
                    icon={Users}
                    title="Tenants"
                    subtitle="Per-tenant queue limits. Tenants exceeding max_queued_runs receive 429 responses."
                />

                <div className="space-y-2 mb-4">
                    {tenants.map(t => (
                        <div key={t.tenant_id} className="flex items-center justify-between border border-zinc-800 p-3">
                            <div>
                                <span className="text-sm text-zinc-300 font-mono">{t.tenant_id}</span>
                                {t.name && <span className="text-xs text-zinc-500 ml-2">{t.name}</span>}
                            </div>
                            <div className="flex items-center gap-4 text-xs text-zinc-500">
                                <span>Max queued: <span className="text-zinc-300">{t.max_queued_runs}</span></span>
                                <span>Max concurrent: <span className="text-zinc-300">{t.max_concurrent_runs}</span></span>
                                <button onClick={() => deleteTenant(t.tenant_id)} className="text-zinc-600 hover:text-red-400 transition-colors">
                                    <Trash2 className="h-3.5 w-3.5" />
                                </button>
                            </div>
                        </div>
                    ))}
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-2 pt-4 border-t border-zinc-800">
                    <input type="text" placeholder="tenant_id" className="bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-zinc-500"
                        value={newTenant.tenant_id} onChange={e => setNewTenant(p => ({ ...p, tenant_id: e.target.value }))} />
                    <input type="text" placeholder="Name (optional)" className="bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-zinc-500"
                        value={newTenant.name} onChange={e => setNewTenant(p => ({ ...p, name: e.target.value }))} />
                    <input type="number" placeholder="Max queued" className="bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-zinc-500"
                        value={newTenant.max_queued_runs} onChange={e => setNewTenant(p => ({ ...p, max_queued_runs: Number(e.target.value) }))} />
                    <button onClick={addTenant} disabled={addingTenant || !newTenant.tenant_id}
                        className="flex items-center justify-center gap-2 px-4 py-2 text-xs font-bold uppercase tracking-wider border border-zinc-700 text-zinc-300 hover:text-zinc-100 disabled:opacity-40 transition-colors">
                        {addingTenant ? <Loader2 className="h-3 w-3 animate-spin" /> : <Plus className="h-3 w-3" />}
                        Add
                    </button>
                </div>
            </div>

            {/* ── Section 6: Observability ────────────────────────────────── */}
            <div className="border border-zinc-800 p-6">
                <SectionHeader
                    icon={Activity}
                    title="Observability"
                    subtitle="OpenTelemetry traces (Jaeger) and Prometheus metrics for distributed debugging."
                />

                <div className="space-y-4">
                    <div>
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">OTLP Endpoint</label>
                        <input type="text" placeholder="http://jaeger:4317" className="w-full bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 font-mono focus:outline-none focus:border-zinc-500" {...field('otlp_endpoint')} />
                        <p className="text-[10px] text-zinc-600 mt-1">Leave empty to disable tracing. When set, every run gets a distributed trace propagated through API server → Redis → Worker → Postgres.</p>
                    </div>
                    <div>
                        <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">Metrics Token</label>
                        <input type="password" placeholder="Bearer token for /metrics endpoint" className="w-full bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 font-mono focus:outline-none focus:border-zinc-500" {...field('metrics_token')} />
                        <p className="text-[10px] text-zinc-600 mt-1">Prometheus scrapes <span className="font-mono text-zinc-400">GET /metrics</span> with this as a Bearer token.</p>
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">Max Global Queue Depth</label>
                            <input type="number" className="w-full bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-zinc-500" {...field('max_global_queue_depth')} />
                            <p className="text-[10px] text-zinc-600 mt-1">Returns 503 when exceeded (backpressure).</p>
                        </div>
                        <div>
                            <label className="text-xs uppercase font-bold text-zinc-500 tracking-wider block mb-1.5">Rate Limit (req/s per tenant)</label>
                            <input type="number" className="w-full bg-zinc-900 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-zinc-500" {...field('rate_limit_per_tenant_rps')} />
                            <p className="text-[10px] text-zinc-600 mt-1">Returns 429 when exceeded.</p>
                        </div>
                    </div>
                </div>
            </div>

            {/* Save Button */}
            <div className="flex items-center gap-4 pt-2">
                <button
                    onClick={handleSave}
                    disabled={isSaving}
                    className="flex items-center gap-2 px-6 py-2.5 text-sm font-bold uppercase tracking-wider bg-zinc-100 text-zinc-950 hover:bg-white disabled:opacity-40 transition-colors"
                >
                    {isSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                    Save Scale Config
                </button>
                {saveMsg && <p className="text-xs text-zinc-400">{saveMsg}</p>}
            </div>
        </div>
    );
}
