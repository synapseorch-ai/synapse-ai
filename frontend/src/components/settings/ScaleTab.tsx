"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import {
    CheckCircle2, XCircle, RefreshCw, Trash2, AlertTriangle,
    Activity, Zap, Cloud,
    ChevronDown, ChevronRight, Search, Copy, BookOpen, X,
} from 'lucide-react';

import { Button, EmptyState, Field, IconButton, Input, Section, Spinner, StatStrip, Switch } from '@/components/ui';
import { cn } from '@/lib/cn';
import { usd } from '@/lib/money';
import { FIELD_LABEL, FORM } from './form';
import { SaveBar } from './SaveBar';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ScaleConfig {
    /** True when METRICS_TOKEN is set. The token itself is never sent (F58). */
    metrics_token_set?: boolean;
    /** True when an S3 secret is stored. The secret itself is never sent (F95). */
    s3_secret_access_key_set?: boolean;
    /** Field → environment variable, for the settings only the environment sets. */
    env_only?: Record<string, string>;
    redis_url: string;
    scale_postgres_url: string;
    scale_mode_enabled: boolean;
    worker_concurrency: number;
    otlp_endpoint: string;
    metrics_token: string;
    max_global_queue_depth: number;
    rate_limit_per_tenant_rps: number;
    pgbouncer_mode: boolean;
    s3_bucket: string;
    s3_region: string;
    s3_prefix: string;
    s3_access_key_id: string;
    s3_secret_access_key: string;
    s3_endpoint_url: string;
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

interface AnalyticsData {
    available: boolean;
    total_runs: number;
    runs_today: number;
    status_counts: Record<string, number>;
    success_rate: number;
    avg_cost_usd: number;
    total_cost_usd_today: number;
    avg_duration_seconds: number;
    cache_hit_rate: number;
    workers_online: number;
    error?: string;
}

interface RecentRun {
    run_id: string;
    orchestration_id: string;
    session_id: string | null;
    tenant_id: string;
    status: string;
    started_at: string | null;
    ended_at: string | null;
    total_cost_usd: number | null;
    total_tokens_used: number | null;
    worker_id: string | null;
}

interface RunDetail extends RecentRun {
    current_step_id: string | null;
    waiting_for_human: boolean;
    human_prompt: string | null;
    cache_hit_count: number | null;
    estimated_savings_usd: number | null;
}

const DEFAULT_CONFIG: ScaleConfig = {
    redis_url: '',
    scale_postgres_url: '',
    scale_mode_enabled: false,
    worker_concurrency: 10,
    otlp_endpoint: '',
    metrics_token: '',
    max_global_queue_depth: 1000000,
    rate_limit_per_tenant_rps: 1000,
    pgbouncer_mode: false,
    s3_bucket: '',
    s3_region: 'us-east-1',
    s3_prefix: 'synapse',
    s3_access_key_id: '',
    s3_secret_access_key: '',
    s3_endpoint_url: '',
};

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function formatDuration(startedAt: string | null, endedAt: string | null): string {
    if (!startedAt) return '—';
    const start = new Date(startedAt).getTime();
    const end = endedAt ? new Date(endedAt).getTime() : Date.now();
    const secs = Math.floor((end - start) / 1000);
    if (secs < 60) return `${secs}s`;
    const mins = Math.floor(secs / 60);
    return `${mins}m ${secs % 60}s`;
}

function copyToClipboard(text: string) {
    navigator.clipboard.writeText(text).catch(() => {});
}


// ---------------------------------------------------------------------------
// Helper components
// ---------------------------------------------------------------------------

/** A state as a small dot and a word (UX1): success is quiet, red only for a failure. */
function StatusBadge({ status }: { status: string }) {
    const dots: Record<string, string> = {
        online: 'bg-success',
        offline: 'bg-danger',
        draining: 'bg-warning',
        ok: 'bg-success',
        error: 'bg-danger',
        completed: 'bg-success',
        running: 'bg-machine',
        failed: 'bg-danger',
        paused: 'bg-person',
        cancelled: 'bg-text-faint',
    };
    const word = status ? status.charAt(0).toUpperCase() + status.slice(1) : status;
    return (
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-text-muted">
            <span aria-hidden className={cn('size-1.5 shrink-0 rounded-full', dots[status] || 'bg-text-faint')} />
            {word}
        </span>
    );
}

/** A connection test's result, as one quiet line under the field. */
function TestResult({ status }: { status: { ok: boolean; msg: string } }) {
    return (
        <p className={cn('flex items-center gap-1.5 text-xs', status.ok ? 'text-success' : 'text-danger')}>
            {status.ok ? <CheckCircle2 className="size-3 shrink-0" aria-hidden /> : <XCircle className="size-3 shrink-0" aria-hidden />}
            {status.msg}
        </p>
    );
}

/** The two-column fields of a run's detail card. */
function DetailRow({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
    return (
        <div className={cn('flex min-w-0 justify-between gap-2', className)}>
            <span className="shrink-0 text-text-faint">{label}</span>
            {children}
        </div>
    );
}

/**
 * A setting the scale layer reads from the environment and nowhere else. It is
 * shown — the value in effect is worth knowing — but not as an input: saving
 * one used to report success while nothing changed (F58). The route names the
 * variable in its `env_only` map, so this never restates it.
 */
function EnvValue({ label, value, envVar, hint }: {
    label: string; value: string; envVar?: string; hint?: React.ReactNode;
}) {
    return (
        <Field label={label} hint={<>
            Set by <span className="font-code text-text-muted">{envVar ?? 'the environment'}</span> in the
            environment; change it there and restart. {hint}
        </>}>
            <Input readOnly value={value} className="cursor-default bg-surface-2 font-code text-text-muted" />
        </Field>
    );
}

// ---------------------------------------------------------------------------
// Scale Docs Drawer
// ---------------------------------------------------------------------------

const CodeBlock = ({ code }: { code: string }) => {
    const [copied, setCopied] = useState(false);
    const copy = async () => {
        await navigator.clipboard.writeText(code);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };
    return (
        <div className="relative group">
            <pre className="bg-surface-2 border border-border p-3 text-xs text-text-muted overflow-x-auto font-code leading-relaxed whitespace-pre-wrap break-words rounded-md">
                {code}
            </pre>
            <button
                onClick={copy}
                className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity px-2 py-1 bg-surface hover:bg-surface-2 text-text-muted hover:text-text text-xs font-medium border border-border-strong rounded-md"
            >
                {copied ? '✓ Copied' : 'Copy'}
            </button>
        </div>
    );
};

const DocSection = ({ title, children, defaultOpen = false }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) => {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="rounded-md border border-border">
            <button
                onClick={() => setOpen(o => !o)}
                className="w-full flex items-center justify-between px-4 py-3 hover:bg-row-hover transition-colors text-left rounded-md"
            >
                <span className="text-sm font-medium text-text">{title}</span>
                {open ? <ChevronDown className="w-3.5 h-3.5 text-text-faint" /> : <ChevronRight className="w-3.5 h-3.5 text-text-faint" />}
            </button>
            {open && <div className="p-4 space-y-4 border-t border-border">{children}</div>}
        </div>
    );
};

/** One group of the environment reference: its name, then each variable over a hairline. */
const EnvGroup = ({ title, rows }: { title: string; rows: string[][] }) => (
    <div>
        <p className={cn(FIELD_LABEL, 'mb-2 font-medium')}>{title}</p>
        <div className="space-y-1 text-xs">
            {rows.map(([k, v]) => (
                <div key={k} className="flex gap-3 py-1.5 border-b border-border">
                    <span className="font-code text-text shrink-0 w-44">{k}</span>
                    <span className="text-text-faint">{v}</span>
                </div>
            ))}
        </div>
    </div>
);

const ScaleDocsDrawer = ({ open, onClose }: { open: boolean; onClose: () => void }) => {
    useEffect(() => {
        const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        if (open) document.addEventListener('keydown', handler);
        return () => document.removeEventListener('keydown', handler);
    }, [open, onClose]);

    return (
        <>
            <div
                className={`fixed inset-0 z-40 bg-scrim/50 transition-opacity duration-300 ${open ? 'opacity-100' : 'opacity-0 pointer-events-none'}`}
                onClick={onClose}
            />
            {/* Closed, the guide is only moved off-screen for the slide, so it is
                inert too: nothing in it takes focus or is read out (F179). */}
            <div
                role="dialog"
                aria-label="Scale deployment guide"
                inert={!open}
                className={`fixed top-0 right-0 z-50 h-full w-full md:w-3/4 bg-bg border-l border-border flex flex-col shadow-2xl transition-transform duration-300 ease-in-out ${open ? 'translate-x-0' : 'translate-x-full'}`}
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-border shrink-0">
                    <div className="flex items-center gap-3">
                        <BookOpen className="w-4 h-4 text-text-faint" aria-hidden />
                        <h2 className="text-[15px] font-semibold text-text">Scale deployment guide</h2>
                    </div>
                    <IconButton label="Close the deployment guide" icon={X} size="sm" onClick={onClose} />
                </div>

                {/* Content */}
                <div className="flex-1 overflow-y-auto p-6 space-y-3 modern-scrollbar">

                    {/* Intro banner */}
                    <div className="flex items-start gap-3 px-4 py-3 bg-accent-subtle border border-border text-xs leading-relaxed text-text rounded-md">
                        <Zap className="w-3.5 h-3.5 text-accent-ink shrink-0 mt-0.5" aria-hidden />
                        <span>
                            Scale mode turns Synapse into a <strong className="font-semibold">distributed job queue</strong>. Your main Synapse instance manages definitions and the UI — workers pull jobs from Redis and execute them in parallel. All you need to get started is a <strong className="font-semibold">Redis server</strong>.
                        </span>
                    </div>

                    <DocSection title="Architecture overview" defaultOpen>
                        <p className="text-xs text-text-muted leading-relaxed">
                            In scale mode, Synapse splits into three roles. You always run exactly one <strong className="font-medium text-text">main instance</strong> (the combined image you already have). Workers and API servers are optional replicas.
                        </p>
                        <div className="space-y-2 text-xs">
                            <div className="border border-border p-3 rounded-md">
                                <p className="text-text font-medium mb-1"><span className="font-code">synapseorchai/synapse-ai</span>  <span className="text-text-faint font-normal">(your existing install)</span></p>
                                <p className="text-text-muted">→ Hosts the UI and settings</p>
                                <p className="text-text-muted">→ Manages orchestrations, agents, tools</p>
                                <p className="text-text-muted">→ Enqueues jobs to Redis when V2 API is called</p>
                                <p className="text-text-muted">→ Streams SSE events back to clients from Redis</p>
                            </div>
                            <div className="border border-border p-3 rounded-md">
                                <p className="text-text font-medium mb-1"><span className="font-code">synapseorchai/synapse-ai-worker</span>  <span className="text-text-faint font-normal">(scale out ×N)</span></p>
                                <p className="text-text-muted">→ Pulls jobs from Redis ARQ queue</p>
                                <p className="text-text-muted">→ Reads definitions from Postgres</p>
                                <p className="text-text-muted">→ Executes orchestrations and agent chat</p>
                                <p className="text-text-muted">→ Publishes step events back to Redis Streams</p>
                                <p className="text-text-muted">→ Health endpoint on <span className="font-code">:9000/health</span></p>
                            </div>
                            <div className="border border-border p-3 rounded-md">
                                <p className="text-text font-medium mb-1"><span className="font-code">synapseorchai/synapse-ai-api-server</span>  <span className="text-text-faint font-normal">(optional — scale API tier)</span></p>
                                <p className="text-text-muted">→ Same API as the main instance but no UI</p>
                                <p className="text-text-muted">→ Useful when you want to scale API replicas separately</p>
                                <p className="text-text-muted">→ Put behind a load balancer alongside the main instance</p>
                            </div>
                        </div>
                    </DocSection>

                    <DocSection title="What you need" defaultOpen>
                        <div className="space-y-3 text-xs">
                            <div className="flex items-start gap-3 p-3 border border-border rounded-md">
                                <span className="text-success font-medium shrink-0 mt-0.5">✓ Required</span>
                                <div>
                                    <p className="text-text font-medium">Redis</p>
                                    <p className="text-text-muted mt-0.5">Job queue, cancellation signals, SSE event streams. Any Redis 6+ works — managed (Upstash, Redis Cloud, ElastiCache) or self-hosted.</p>
                                    <CodeBlock code={`# Quickest self-hosted option:\ndocker run -d -p 6379:6379 redis:7-alpine`} />
                                </div>
                            </div>
                            <div className="flex items-start gap-3 p-3 border border-border rounded-md">
                                <span className="text-success font-medium shrink-0 mt-0.5">✓ Required</span>
                                <div>
                                    <p className="text-text font-medium">Postgres</p>
                                    <p className="text-text-muted mt-0.5">Orchestrations, agents and tools live here, and workers write run state here. This instance reads and writes the same database, so there is nothing to push. Any Postgres 14+ works.</p>
                                </div>
                            </div>
                            <div className="flex items-start gap-3 p-3 border border-border rounded-md">
                                <span className="text-text-faint font-medium shrink-0 mt-0.5">○ Optional</span>
                                <div>
                                    <p className="text-text font-medium">S3 (or compatible)</p>
                                    <p className="text-text-muted mt-0.5">When configured, the vault, tool outputs and run logs are stored in S3, and every worker reads the same files. Without it, each process uses its own disk (<span className="font-code">SYNAPSE_BLOB_DIR</span>) — fine on one machine; across several hosts that directory has to be shared.</p>
                                </div>
                            </div>
                        </div>
                    </DocSection>

                    <DocSection title="Quick start — Docker run" defaultOpen>
                        <p className="text-xs text-text-muted">The fastest way to add a worker to your existing Synapse install. Set the two required env vars and pull the image.</p>
                        <CodeBlock code={`# 1. Pull the worker image
docker pull synapseorchai/synapse-ai-worker:latest

# 2. Run it — point at your Redis and Postgres
docker run -d \\
  --name synapse-worker-1 \\
  -e REDIS_URL="redis://your-redis-host:6379/0" \\
  -e SCALE_POSTGRES_URL="postgresql://user:pass@your-pg-host:5432/synapse" \\
  -e WORKER_CONCURRENCY=10 \\
  -p 9000:9000 \\
  synapseorchai/synapse-ai-worker:latest

# 3. Check it registered
curl http://localhost:9000/health
# {"status":"ok","worker_id":"...","active_jobs":0,"uptime":12.3}`} />
                        <p className="text-xs text-text-muted">Scale horizontally by running this command on multiple machines — all workers share the same Redis queue.</p>
                    </DocSection>

                    <DocSection title="Quick start — Docker Compose">
                        <p className="text-xs text-text-muted">Add workers alongside your existing stack using the built-in scale profile.</p>
                        <CodeBlock code={`# In your .env file, add:
SCALE_POSTGRES_URL=postgresql://user:pass@your-pg-host:5432/synapse

# Then start with the scale profile:
docker compose --profile scale up -d

# This starts:
#   synapse-backend    — your API server
#   synapse-frontend   — your UI
#   synapse-redis      — local Redis (auto-provisioned)
#   synapse-worker     — 1 worker (add replicas to scale)`} />
                        <CodeBlock code={`# To run multiple workers (scale replicas):
docker compose --profile scale up -d --scale worker=4`} />
                    </DocSection>

                    <DocSection title="Kubernetes deployment">
                        <p className="text-xs text-text-muted">Pre-built manifests are in <code className="font-code text-text">infra/k8s/</code> in the repo. Apply them after creating a <code className="font-code text-text">synapse-secrets</code> Secret.</p>
                        <CodeBlock code={`# 1. Create the secrets
kubectl create secret generic synapse-secrets \\
  --from-literal=redis-url="redis://your-redis:6379/0" \\
  --from-literal=postgres-url="postgresql://user:pass@your-pg:5432/synapse"

# 2. Apply the manifests
kubectl apply -f infra/k8s/

# This deploys:
#   synapse-api       — API server (3 replicas, HPA 2-20)
#   synapse-worker    — workers (KEDA autoscales 1-100 based on queue depth)
#   synapse-pgbouncer — connection pooling for Postgres`} />
                        <CodeBlock code={`# KEDA autoscales workers based on Redis queue depth.
# When queue > 5 jobs, a new worker pod spins up (up to 100).
# Requires KEDA installed in your cluster:
kubectl apply -f https://github.com/kedacore/keda/releases/download/v2.13.0/keda-2.13.0.yaml`} />
                        <p className="text-xs text-text-muted">The worker manifest uses <code className="font-code text-text">synapseorchai/synapse-ai-worker:latest</code> with <code className="font-code text-text">imagePullPolicy: Always</code> so new versions roll out automatically on pod restart.</p>
                    </DocSection>

                    <DocSection title="Environment variables reference">
                        <p className="text-xs text-text-muted mb-3">All variables supported by the worker image. Pass via <code className="font-code text-text">-e</code>, <code className="font-code text-text">--env-file</code>, or k8s Secret/ConfigMap.</p>
                        <div className="space-y-4">
                            <EnvGroup title="Required" rows={[
                                ['REDIS_URL', 'redis://host:6379/0  or  redis+cluster://h1:6379,h2:6379'],
                                ['SCALE_POSTGRES_URL', 'postgresql://user:pass@host:5432/dbname'],
                            ]} />
                            <EnvGroup title="Worker tuning" rows={[
                                ['WORKER_CONCURRENCY', '10 — max parallel jobs per worker'],
                                ['WORKER_JOB_TIMEOUT', '3600 — seconds before a job times out'],
                                ['WORKER_MAX_RETRIES', '3 — tries a job gets when a stopping worker cuts it off; each resumes from its checkpoint, and after the last the run is failed and dead-lettered'],
                                ['WORKER_HEALTH_PORT', '9000 — HTTP health endpoint port'],
                            ]} />
                            <EnvGroup title="S3 storage (optional)" rows={[
                                ['S3_BUCKET', 'my-synapse-bucket'],
                                ['S3_REGION', 'us-east-1'],
                                ['S3_PREFIX', 'synapse — every key is stored under <prefix>/<tenant>/'],
                                ['S3_ACCESS_KEY_ID', 'AKIA... (blank = use IAM role)'],
                                ['S3_SECRET_ACCESS_KEY', ''],
                                ['S3_ENDPOINT_URL', 'https://... (MinIO / R2 / etc.)'],
                            ]} />
                            <EnvGroup title="Observability (optional)" rows={[
                                ['OTLP_ENDPOINT', 'your collector — telemetry is off until set'],
                                ['SYNAPSE_TELEMETRY', 'otlp: send to OTEL_EXPORTER_OTLP_ENDPOINT'],
                                ['METRICS_TOKEN', 'bearer token for /api/v2/metrics'],
                                ['K8S_MODE', '1 when running on Kubernetes'],
                                ['PGBOUNCER_MODE', '1 when Postgres URL points to PgBouncer'],
                            ]} />
                        </div>
                    </DocSection>

                    <DocSection title="Setup checklist">
                        <div className="space-y-2 text-xs">
                            {[
                                ['1', 'Connect Redis URL in the Redis connection section above and click Test'],
                                ['2', 'Enable scale mode toggle'],
                                ['3', 'Connect Postgres URL in the Postgres section and click Test'],
                                ['4', 'Pull and start the worker image (Docker run or Docker Compose above)'],
                                ['5', 'Confirm the worker appears in the Workers panel (it self-registers on startup)'],
                                ['6', 'Trigger a V2 run via API and watch the Live queue counter increment → decrement'],
                            ].map(([n, text]) => (
                                <div key={n} className="flex items-start gap-3 py-2 border-b border-border last:border-b-0">
                                    <span className="w-5 h-5 rounded-full border border-border-strong text-text-faint text-xs font-medium tabular-nums flex items-center justify-center shrink-0">{n}</span>
                                    <span className="text-text-muted">{text}</span>
                                </div>
                            ))}
                        </div>
                    </DocSection>

                </div>
            </div>
        </>
    );
};

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ScaleTab() {
    const [config, setConfig] = useState<ScaleConfig>(DEFAULT_CONFIG);
    const [isSaving, setIsSaving] = useState(false);
    const [saveMsg, setSaveMsg] = useState('');
    const [docsOpen, setDocsOpen] = useState(false);

    // Connection test state
    const [redisTesting, setRedisTesting] = useState(false);
    const [redisStatus, setRedisStatus] = useState<{ ok: boolean; msg: string } | null>(null);
    const [pgTesting, setPgTesting] = useState(false);
    const [pgStatus, setPgStatus] = useState<{ ok: boolean; msg: string } | null>(null);
    const [s3Testing, setS3Testing] = useState(false);
    const [s3Status, setS3Status] = useState<{ ok: boolean; msg: string } | null>(null);

    // Workers
    const [workers, setWorkers] = useState<WorkerInfo[]>([]);
    const [workerHealthResults, setWorkerHealthResults] = useState<Record<string, any>>({});

    // Queue stats
    const [queueStats, setQueueStats] = useState<QueueStats | null>(null);

    // DLQ
    const [dlqEntries, setDlqEntries] = useState<DLQEntry[]>([]);
    const [dlqLoading, setDlqLoading] = useState(false);
    const [retryingDlq, setRetryingDlq] = useState<string | null>(null);
    const [dlqRefused, setDlqRefused] = useState<{ id: string; message: string } | null>(null);
    const [expandedDlq, setExpandedDlq] = useState<string | null>(null);


    // Analytics & monitoring (new)
    const [analytics, setAnalytics] = useState<AnalyticsData | null>(null);
    const [recentRuns, setRecentRuns] = useState<RecentRun[]>([]);
    const [searchQuery, setSearchQuery] = useState('');
    const [searchRunDetail, setSearchRunDetail] = useState<RunDetail | null>(null);
    const [searchSessionRuns, setSearchSessionRuns] = useState<RecentRun[] | null>(null);
    const [isSearching, setIsSearching] = useState(false);
    const [searchError, setSearchError] = useState<string | null>(null);

    // Collapsible config sections (all collapsed by default)
    const [expandedConfigs, setExpandedConfigs] = useState<Record<string, boolean>>({
        redis: false, postgres: false, s3: false, workers: false, observability: false,
    });

    const toggleSection = (key: string) =>
        setExpandedConfigs(prev => ({ ...prev, [key]: !prev[key] }));

    // Load on mount
    useEffect(() => {
        fetch('/api/scale/config')
            .then(r => r.json())
            .then(data => setConfig({ ...DEFAULT_CONFIG, ...data }))
            .catch(() => {});

        loadWorkers();
        loadQueueStats();
        loadDlq();
        loadAnalytics();
        loadRecentRuns();
    }, []);

    // Poll queue stats + analytics every 5s when scale mode is on
    useEffect(() => {
        if (!config.scale_mode_enabled) return;
        const interval = setInterval(() => {
            loadQueueStats();
            loadAnalytics();
            loadRecentRuns();
        }, 10000);
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

    const loadAnalytics = useCallback(() => {
        fetch('/api/scale/analytics')
            .then(r => r.json())
            .then(data => data && typeof data === 'object' ? setAnalytics(data) : null)
            .catch(() => {});
    }, []);

    const loadRecentRuns = useCallback(() => {
        fetch('/api/scale/runs?limit=20')
            .then(r => r.json())
            .then(data => setRecentRuns(Array.isArray(data) ? data : []))
            .catch(() => setRecentRuns([]));
    }, []);

    const handleSearch = async (q?: string) => {
        const query = (q ?? searchQuery).trim();
        if (!query) return;
        setIsSearching(true);
        setSearchError(null);
        setSearchRunDetail(null);
        setSearchSessionRuns(null);
        try {
            if (query.startsWith('sess_')) {
                const r = await fetch(`/api/scale/runs?session_id=${encodeURIComponent(query)}&limit=50`);
                if (!r.ok) throw new Error('Not found');
                const data = await r.json();
                setSearchSessionRuns(Array.isArray(data) ? data : []);
            } else {
                const r = await fetch(`/api/scale/runs/${encodeURIComponent(query)}`);
                if (r.status === 404) throw new Error(`Run "${query}" not found`);
                if (!r.ok) throw new Error('Search failed');
                setSearchRunDetail(await r.json());
            }
        } catch (e: any) {
            setSearchError(e.message || 'Search failed');
        } finally {
            setIsSearching(false);
        }
    };

    const handleSave = async () => {
        setIsSaving(true);
        setSaveMsg('');
        try {
            // Only what saving can change. The environment-only settings and the
            // route's own annotations are left out rather than sent to be ignored.
            const envOnly = new Set(Object.keys(config.env_only ?? {}));
            const body = Object.fromEntries(Object.entries(config).filter(
                ([k]) => !envOnly.has(k) && k !== 'env_only' && k !== 'metrics_token_set'
                    && k !== 's3_secret_access_key_set'));
            const res = await fetch('/api/scale/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                const detail = await res.json().catch(() => ({}));
                setSaveMsg(detail.detail || `Save failed (${res.status}).`);
                return;
            }
            setSaveMsg('Saved. Restart the server and every worker to apply Redis, Postgres or S3 changes.');
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

    const testS3 = async () => {
        setS3Testing(true);
        setS3Status(null);
        try {
            const r = await fetch('/api/scale/test-s3', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    s3_bucket: config.s3_bucket,
                    s3_region: config.s3_region,
                    s3_prefix: config.s3_prefix,
                    s3_access_key_id: config.s3_access_key_id,
                    s3_secret_access_key: config.s3_secret_access_key,
                    s3_endpoint_url: config.s3_endpoint_url,
                }),
            });
            const d = await r.json();
            setS3Status({ ok: d.ok, msg: d.ok ? 'Connection successful' : (d.error || 'Connection failed') });
        } catch (e) {
            setS3Status({ ok: false, msg: String(e) });
        } finally {
            setS3Testing(false);
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
        setDlqRefused(null);
        try {
            const res = await fetch(`/api/scale/dlq/${dlqId}/retry`, { method: 'POST' });
            // The route refuses a run that never reached a step, one that is not
            // failed or cancelled, and one already queued — each with the reason.
            // Redrawing the list as though it had worked hid all three (F58).
            if (!res.ok) {
                const body = await res.json().catch(() => ({}));
                setDlqRefused({ id: dlqId, message: body.detail || `The retry was refused (${res.status}).` });
                return;
            }
            loadDlq();
        } finally {
            setRetryingDlq(null);
        }
    };

    const field = (key: keyof ScaleConfig) => ({
        value: String(config[key] ?? ''),
        onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
            setConfig(prev => ({ ...prev, [key]: e.target.type === 'number' ? Number(e.target.value) : e.target.value })),
    });


    // The kit Switch hands back the new value rather than an input event.
    const toggle = (key: keyof ScaleConfig) => ({
        checked: Boolean(config[key]),
        onChange: (checked: boolean) =>
            setConfig(prev => ({ ...prev, [key]: checked })),
    });

    const openRun = (runId: string) => { setSearchQuery(runId); handleSearch(runId); };
    const RUN_GRID = 'grid grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)_minmax(0,1.2fr)_minmax(0,1fr)_minmax(0,0.8fr)_minmax(0,0.8fr)] items-center gap-x-4';

    // ---------------------------------------------------------------------------
    // Render
    // ---------------------------------------------------------------------------

    return (
        <div>

            {/* Docs Drawer */}
            <ScaleDocsDrawer open={docsOpen} onClose={() => setDocsOpen(false)} />

            {/* ── Analytics ─────────────────────────────────────────────────── */}
            <Section variant="plain" collapsible={false} title="Scale analytics"
                actions={<>
                    {analytics && !analytics.available && (
                        <span className="text-xs text-text-faint">(requires Postgres)</span>
                    )}
                    <Button variant="secondary" size="sm" onClick={() => setDocsOpen(true)}>
                        <BookOpen className="size-3.5" aria-hidden />
                        Deploy docs
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => { loadAnalytics(); loadRecentRuns(); loadQueueStats(); }}>
                        <RefreshCw className="size-3.5" aria-hidden /> Refresh
                    </Button>
                </>}
            >
                {/* `StatStrip` carries `m-0`, which beats a parent's `space-y` (M2e). */}
                <div className={`${FORM} space-y-4`}>
                    <div>
                        <StatStrip size="sm" label="Runs" cells={[
                            { label: 'Total runs', value: analytics?.available ? analytics.total_runs : '—' },
                            { label: 'Runs today', value: analytics?.available ? analytics.runs_today : '—' },
                            { label: 'Active now', value: analytics?.available ? (analytics.status_counts?.running ?? 0) : (queueStats?.active ?? '—'), dot: 'machine' },
                            { label: 'Success rate', value: analytics?.available ? `${analytics.success_rate}%` : '—', dot: analytics?.available && (analytics.success_rate ?? 0) >= 90 ? 'success' : undefined },
                        ]} />
                    </div>
                    <div>
                        <StatStrip size="sm" label="Workers and cost" cells={[
                            { label: 'Workers online', value: analytics?.available ? analytics.workers_online : (workers.filter(w => w.status === 'online').length || '—'), dot: 'success' },
                            { label: 'Avg cost / run', value: analytics?.available ? usd(analytics.avg_cost_usd) : '—' },
                            { label: 'Cost today', value: analytics?.available ? usd(analytics.total_cost_usd_today) : '—' },
                            { label: 'Cache hit rate', value: analytics?.available ? `${analytics.cache_hit_rate}%` : '—' },
                        ]} />
                    </div>
                </div>
            </Section>

            {/* ── Search ────────────────────────────────────────────────────── */}
            <Section variant="plain" collapsible={false} title="Search by run ID or session ID">
                <div className={`${FORM} space-y-3`}>
                    <label htmlFor="scale-run-search" className="sr-only">Search by run ID or session ID</label>
                    <div className="flex gap-2">
                        <div className="relative flex-1">
                            <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-text-faint" aria-hidden />
                            <Input
                                id="scale-run-search"
                                type="text"
                                placeholder="run_abc_1234567890 or sess_xyz..."
                                className="w-full pl-8 font-code"
                                value={searchQuery}
                                onChange={e => setSearchQuery(e.target.value)}
                                onKeyDown={e => e.key === 'Enter' && handleSearch()}
                            />
                        </div>
                        <Button
                            variant="secondary"
                            onClick={() => handleSearch()}
                            loading={isSearching}
                            disabled={!searchQuery.trim()}
                        >
                            {!isSearching && <Search className="size-3.5" aria-hidden />}
                            Search
                        </Button>
                    </div>
                    <p className="text-xs text-text-faint">Prefix <span className="font-code text-text-muted">sess_</span> to search by session ID and see all runs in that session.</p>

                    {/* Search error */}
                    {searchError && (
                        <p className="text-xs text-danger flex items-center gap-1.5">
                            <XCircle className="h-3 w-3" aria-hidden /> {searchError}
                        </p>
                    )}

                    {/* Run detail result */}
                    {searchRunDetail && (
                        <div className="rounded-md border border-border bg-surface p-4">
                            <div className="flex items-start justify-between gap-4 mb-3">
                                <div className="flex items-center gap-3 flex-wrap min-w-0">
                                    <button
                                        onClick={() => copyToClipboard(searchRunDetail.run_id)}
                                        className="font-code text-xs text-text hover:text-accent-ink flex items-center gap-1 truncate rounded-sm"
                                    >
                                        {searchRunDetail.run_id}
                                        <Copy className="h-3 w-3 shrink-0 text-text-faint" />
                                    </button>
                                    <StatusBadge status={searchRunDetail.status} />
                                </div>
                                <button onClick={() => setSearchRunDetail(null)} className="text-text-faint hover:text-text text-xs shrink-0 rounded-sm">✕</button>
                            </div>
                            <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
                                <DetailRow label="Orchestration">
                                    <span className="text-text truncate">{searchRunDetail.orchestration_id || '—'}</span>
                                </DetailRow>
                                <DetailRow label="Worker">
                                    <span className="text-text truncate">{searchRunDetail.worker_id || '—'}</span>
                                </DetailRow>
                                <DetailRow label="Duration">
                                    <span className="text-text tabular-nums">{formatDuration(searchRunDetail.started_at, searchRunDetail.ended_at)}</span>
                                </DetailRow>
                                <DetailRow label="Cost">
                                    <span className="text-text tabular-nums">{usd(searchRunDetail.total_cost_usd)}</span>
                                </DetailRow>
                                <DetailRow label="Tokens">
                                    <span className="text-text tabular-nums">{searchRunDetail.total_tokens_used?.toLocaleString() ?? '—'}</span>
                                </DetailRow>
                                <DetailRow label="Cache hits">
                                    <span className="text-text tabular-nums">{searchRunDetail.cache_hit_count ?? '—'}</span>
                                </DetailRow>
                                {searchRunDetail.current_step_id && (
                                    <DetailRow label="Current step" className="col-span-2">
                                        <span className="font-code text-text truncate">{searchRunDetail.current_step_id}</span>
                                    </DetailRow>
                                )}
                                {searchRunDetail.waiting_for_human && (
                                    <div className="col-span-2 flex items-start gap-2 px-3 py-2 bg-person-subtle text-person-ink rounded-md">
                                        <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" aria-hidden />
                                        <span className="text-xs">Waiting for human input{searchRunDetail.human_prompt ? `: ${searchRunDetail.human_prompt}` : ''}</span>
                                    </div>
                                )}
                                {searchRunDetail.estimated_savings_usd != null && searchRunDetail.estimated_savings_usd > 0 && (
                                    <DetailRow label="Cache savings">
                                        <span className="text-success tabular-nums">{usd(searchRunDetail.estimated_savings_usd)}</span>
                                    </DetailRow>
                                )}
                            </div>
                            <div className="mt-3 text-xs text-text-faint">
                                Started: {searchRunDetail.started_at ? new Date(searchRunDetail.started_at).toLocaleString() : '—'}
                                {searchRunDetail.ended_at && ` · Ended: ${new Date(searchRunDetail.ended_at).toLocaleString()}`}
                            </div>
                        </div>
                    )}

                    {/* Session search results */}
                    {searchSessionRuns !== null && (
                        <div>
                            <p className={cn(FIELD_LABEL, 'mb-2')}>
                                {searchSessionRuns.length} run{searchSessionRuns.length !== 1 ? 's' : ''} in session
                            </p>
                            {searchSessionRuns.length === 0 ? (
                                <p className="text-xs text-text-faint text-center py-4">No runs found for this session.</p>
                            ) : (
                                <div className="rounded-md border border-border">
                                    {searchSessionRuns.map(run => (
                                        // A button, so a keyboard opens a run as a click does (F236).
                                        <button
                                            type="button"
                                            key={run.run_id}
                                            className="flex w-full items-center justify-between gap-3 border-b border-border px-3 py-2.5 text-left last:border-b-0 hover:bg-row-hover transition-colors first:rounded-t-md last:rounded-b-md"
                                            onClick={() => openRun(run.run_id)}
                                        >
                                            <span className="flex items-center gap-3 min-w-0">
                                                <span className="font-code text-xs text-text truncate">{run.run_id}</span>
                                                <StatusBadge status={run.status} />
                                            </span>
                                            <span className="flex items-center gap-3 text-xs tabular-nums text-text-muted shrink-0">
                                                <span>{formatDuration(run.started_at, run.ended_at)}</span>
                                                <span>{usd(run.total_cost_usd)}</span>
                                            </span>
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </Section>

            {/* ── Recent runs ───────────────────────────────────────────────── */}
            <Section variant="plain" collapsible={false} title="Recent runs"
                actions={
                    <Button variant="ghost" size="sm" onClick={loadRecentRuns}>
                        <RefreshCw className="size-3.5" aria-hidden /> Refresh
                    </Button>
                }
            >
                {recentRuns.length === 0 ? (
                    <EmptyState title="No runs recorded yet." className={`${FORM} py-8`}>
                        Runs appear here when scale mode is active and Postgres is connected.
                    </EmptyState>
                ) : (
                    <div className="overflow-x-auto">
                        <div role="table" aria-label="Recent runs" className="min-w-[640px] text-[13px]">
                            <div role="rowgroup">
                                <div role="row" className={cn(RUN_GRID, 'border-b border-border px-2.5 pb-2 text-xs text-text-faint')}>
                                    {['Run ID', 'Status', 'Orchestration', 'Started', 'Duration', 'Cost'].map(h => (
                                        <span role="columnheader" key={h} className={h === 'Cost' || h === 'Duration' ? 'text-right' : undefined}>{h}</span>
                                    ))}
                                </div>
                            </div>
                            <div role="rowgroup">
                                {recentRuns.map(run => (
                                    <div
                                        role="row"
                                        key={run.run_id}
                                        className={cn(RUN_GRID, 'cursor-pointer border-b border-border px-2.5 py-2 transition-colors hover:bg-row-hover')}
                                        onClick={() => openRun(run.run_id)}
                                    >
                                        <span role="cell" className="flex min-w-0 items-center gap-1.5">
                                            {/* The row opens on a click; its id does the same for a keyboard (F236). */}
                                            <button
                                                type="button"
                                                onClick={e => { e.stopPropagation(); openRun(run.run_id); }}
                                                className="font-code text-xs text-text truncate max-w-[140px] text-left rounded-sm hover:text-accent-ink"
                                                title={run.run_id}
                                                aria-label={`Open run ${run.run_id}`}
                                            >
                                                {run.run_id.length > 20 ? `${run.run_id.slice(0, 20)}…` : run.run_id}
                                            </button>
                                            <button
                                                type="button"
                                                onClick={e => { e.stopPropagation(); copyToClipboard(run.run_id); }}
                                                className="text-text-faint hover:text-text shrink-0 rounded-sm"
                                                aria-label={`Copy run id ${run.run_id}`}
                                                title="Copy run id"
                                            >
                                                <Copy className="h-3 w-3" aria-hidden />
                                            </button>
                                        </span>
                                        <span role="cell"><StatusBadge status={run.status} /></span>
                                        <span role="cell" className="min-w-0">
                                            <span className="text-text-muted truncate block" title={run.orchestration_id}>
                                                {run.orchestration_id ? run.orchestration_id.slice(0, 14) : '—'}
                                            </span>
                                        </span>
                                        <span role="cell" className="text-text-faint whitespace-nowrap tabular-nums">
                                            {run.started_at ? new Date(run.started_at).toLocaleTimeString() : '—'}
                                        </span>
                                        <span role="cell" className="text-right text-text-muted whitespace-nowrap tabular-nums">
                                            {formatDuration(run.started_at, run.ended_at)}
                                        </span>
                                        <span role="cell" className="text-right text-text tabular-nums">{usd(run.total_cost_usd)}</span>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                )}
            </Section>

            {/* ── Live queue ────────────────────────────────────────────────── */}
            <Section variant="plain" collapsible={false} title="Live queue">
                <div className={`${FORM} space-y-2`}>
                    <div>
                        <StatStrip size="sm" label="Live queue" cells={[
                            { label: 'Queued', value: queueStats?.queued ?? '—' },
                            { label: 'Active', value: queueStats?.active ?? '—', dot: 'machine' },
                            { label: 'Failed', value: queueStats?.failed ?? '—', dot: 'danger' },
                            { label: 'DLQ', value: queueStats?.dlq_count ?? '—', dot: 'neutral' },
                        ]} />
                    </div>
                    {queueStats && !queueStats.available && (
                        <p className="text-xs text-text-faint">Redis not connected</p>
                    )}
                </div>
            </Section>

            {/* ── Workers (at a glance) ─────────────────────────────────────── */}
            <Section variant="plain" collapsible={false} title="Workers"
                actions={<IconButton label="Refresh workers" icon={RefreshCw} size="sm" onClick={loadWorkers} />}
            >
                <div className={`${FORM} space-y-3`}>
                    {workers.length === 0 ? (
                        <p className="text-sm text-text-faint">No workers registered</p>
                    ) : (
                        <div className="rounded-md border border-border">
                            {workers.map(w => (
                                <div key={w.worker_id} className="flex items-center justify-between gap-3 border-b border-border px-3 py-2 last:border-b-0">
                                    <div className="flex min-w-0 items-center gap-3">
                                        <StatusBadge status={w.status} />
                                        <span className="text-xs font-code text-text truncate">{w.hostname || w.worker_id.slice(0, 16)}</span>
                                        <span className="shrink-0 text-xs tabular-nums text-text-faint">{w.active_jobs}/{w.max_jobs} jobs</span>
                                    </div>
                                    <IconButton label="Health" icon={Activity} size="sm" onClick={() => checkWorkerHealth(w)} />
                                </div>
                            ))}
                        </div>
                    )}
                    <button
                        onClick={() => toggleSection('workers')}
                        className="text-xs text-accent-ink hover:underline rounded-sm"
                    >
                        Manage workers ↓
                    </button>
                </div>
            </Section>

            {/* ── Failed jobs (DLQ) ─────────────────────────────────────────── */}
            <Section variant="plain" collapsible={false} title="Failed jobs (DLQ)"
                actions={
                    <Button variant="ghost" size="sm" iconOnly onClick={loadDlq} aria-label="Refresh the failed jobs list">
                        {dlqLoading ? <Spinner size={12} /> : <RefreshCw className="size-3.5" aria-hidden />}
                    </Button>
                }
            >
                <div className={FORM}>
                    {dlqEntries.length === 0 ? (
                        <p className="flex items-center gap-2 text-sm text-text-muted">
                            <span aria-hidden className="size-1.5 rounded-full bg-success" /> No failed jobs
                        </p>
                    ) : (
                        <div className="space-y-2">
                            <div className="rounded-md border border-border">
                                {dlqEntries.slice(0, 3).map(entry => (
                                    <div key={entry.id} className="flex items-start justify-between gap-3 border-b border-border px-3 py-2.5 last:border-b-0">
                                        <div className="min-w-0">
                                            <p className="font-code text-xs text-text truncate">{entry.run_id || entry.id}</p>
                                            <p className="text-xs text-danger truncate mt-0.5">{entry.error_message}</p>
                                            <p className="text-xs text-text-faint">{entry.attempt_count} attempts</p>
                                            {dlqRefused?.id === entry.id && (
                                                <p role="alert" className="text-xs text-text mt-1.5 rounded-md border border-warning/25 bg-warning-subtle px-2 py-1">{dlqRefused.message}</p>
                                            )}
                                        </div>
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            iconOnly
                                            onClick={() => retryDlq(entry.id)}
                                            disabled={retryingDlq === entry.id}
                                            aria-label={`Retry ${entry.run_id || entry.id}`}
                                            title="Resume this run from the step it failed at"
                                        >
                                            {retryingDlq === entry.id ? <Spinner size={12} /> : <RefreshCw className="size-3.5" aria-hidden />}
                                        </Button>
                                    </div>
                                ))}
                            </div>
                            {dlqEntries.length > 3 && (
                                <p className="text-xs text-text-faint">+{dlqEntries.length - 3} more — expand Queue section below</p>
                            )}
                        </div>
                    )}
                </div>
            </Section>

            {/* ── Configuration sections (collapsed by default) ─────────────── */}

            <Section variant="plain" title="Redis connection" defaultOpen={expandedConfigs.redis}
                description="Required for distributed workers, queue management, and distributed cancellation."
            >
                <div className={`${FORM} space-y-5`}>
                    <div className="space-y-1.5">
                        <div className="flex items-end gap-2">
                            <Field label="Redis URL" className="flex-1">
                                <Input type="text" placeholder="redis://localhost:6379/0"
                                    className="font-code"
                                    {...field('redis_url')} />
                            </Field>
                            <Button variant="secondary" onClick={testRedis}
                                loading={redisTesting} disabled={!config.redis_url}>
                                Test
                            </Button>
                        </div>
                        {redisStatus && <TestResult status={redisStatus} />}
                        <p className="text-xs text-text-faint">For Redis Cluster: <span className="font-code">redis+cluster://host1:6379,host2:6379</span></p>
                    </div>

                    <div className="flex items-start justify-between gap-6 border-t border-border pt-4">
                        <div>
                            <p className="text-sm text-text font-medium">Enable scale mode</p>
                            <p className="text-xs text-text-muted mt-0.5">Route V2 API calls through Redis ARQ workers instead of in-process execution.</p>
                        </div>
                        <Switch label="Enable scale mode" className="mt-0.5" {...toggle('scale_mode_enabled')} />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <EnvValue label="Worker concurrency" value={String(config.worker_concurrency)}
                            envVar={config.env_only?.worker_concurrency}
                            hint="Max parallel jobs per worker process." />
                    </div>
                </div>
            </Section>

            <Section variant="plain" title="Postgres" defaultOpen={expandedConfigs.postgres}
                description="Workers read orchestrations, agents and tools from the same database this instance writes."
            >
                <div className={`${FORM} space-y-5`}>
                    <div className="space-y-1.5">
                        <div className="flex items-end gap-2">
                            <Field label="Postgres URL (scale)" className="flex-1">
                                <Input type="text" placeholder="postgresql://user:pass@host:5432/synapse"
                                    className="font-code"
                                    {...field('scale_postgres_url')} />
                            </Field>
                            <Button variant="secondary" onClick={testPostgres}
                                loading={pgTesting} disabled={!config.scale_postgres_url}>
                                Test
                            </Button>
                        </div>
                        {pgStatus && <TestResult status={pgStatus} />}
                        <p className="text-xs text-text-faint">Separate from the embed-code Postgres. Behind PgBouncer, point this at the PgBouncer port.</p>
                    </div>

                    <div className="border-t border-border pt-4 max-w-xs">
                        <EnvValue label="PgBouncer mode" value={config.pgbouncer_mode ? 'On' : 'Off'}
                            envVar={config.env_only?.pgbouncer_mode}
                            hint="Uses NullPool, which PgBouncer in transaction mode requires." />
                    </div>
                </div>
            </Section>

            <Section variant="plain" title="Storage (S3)" defaultOpen={expandedConfigs.s3}
                description="When a bucket is set, the vault, tool outputs and run logs live in S3, and every worker reads the same files."
            >
                <div className={`${FORM} space-y-4`}>
                    <div className="grid grid-cols-2 gap-4">
                        <Field label="S3 bucket">
                            <Input type="text" placeholder="my-synapse-bucket" className="font-code" {...field('s3_bucket')} />
                        </Field>
                        <Field label="Region">
                            <Input type="text" placeholder="us-east-1" className="font-code" {...field('s3_region')} />
                        </Field>
                    </div>
                    <Field label="Key prefix" hint="All objects are stored under this prefix.">
                        <Input type="text" placeholder="synapse" className="font-code" {...field('s3_prefix')} />
                    </Field>
                    <div className="grid grid-cols-2 gap-4">
                        <Field label="Access key ID">
                            <Input type="text" placeholder="AKIA... (optional)" className="font-code" {...field('s3_access_key_id')} />
                        </Field>
                        <Field label="Secret access key"
                            hint={config.s3_secret_access_key_set
                                ? 'Stored, and never shown. Leave empty to keep it.'
                                : undefined}>
                            {/* Write-only (F95): the route no longer reads it back,
                                and an empty field keeps what is stored. */}
                            <Input type="password" autoComplete="new-password"
                                placeholder={config.s3_secret_access_key_set ? '•••••••• stored' : '••••••••'}
                                className="font-code" {...field('s3_secret_access_key')} />
                        </Field>
                    </div>
                    <Field label="Endpoint URL" hint="Optional — for MinIO, Cloudflare R2, and other S3-compatible stores.">
                        <Input type="text" placeholder="https://account.r2.cloudflarestorage.com"
                            className="font-code"
                            {...field('s3_endpoint_url')} />
                    </Field>
                    <div className="flex flex-wrap items-center gap-3 pt-1">
                        <Button variant="secondary" onClick={testS3}
                            loading={s3Testing} disabled={!config.s3_bucket}>
                            {!s3Testing && <Cloud className="size-3.5" aria-hidden />}
                            Test S3 connection
                        </Button>
                        {s3Status && <TestResult status={s3Status} />}
                    </div>
                    <p className="text-xs text-text-faint">Leave the access key ID blank to use an IAM role or environment credentials — saving without one also clears a stored secret.</p>
                    {/* F57: the bucket became the real blob store in batch 1C. It is
                        chosen when each process starts, and reads go to it alone. */}
                    <div className="flex items-start gap-2 rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5 text-xs text-text">
                        <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px text-warning" aria-hidden />
                        <p>
                            A bucket takes effect when the server and every worker restart. Files already on
                            local disk are not copied: after the switch the vault starts empty in S3, and what
                            was there stays in <span className="font-code">SYNAPSE_BLOB_DIR</span> until uploaded again.
                        </p>
                    </div>
                </div>
            </Section>

            {/* "Manage workers ↓" flips `expandedConfigs.workers`; the key remounts the
                section so it opens or closes with it, as the hand-written one did. */}
            <Section key={`workers-${expandedConfigs.workers}`} variant="plain" title="Workers" defaultOpen={expandedConfigs.workers}
                description="Remote worker instances running Dockerfile.worker. Each worker pulls jobs from Redis ARQ."
            >
                <div className={`${FORM} space-y-3`}>
                    {workers.length === 0 ? (
                        <EmptyState title="No workers have reported in." className="py-8">
                            A worker appears here on its first heartbeat. Start one from <span className="font-code text-text">Dockerfile.worker</span>, pointed at the same Redis and database.
                        </EmptyState>
                    ) : (
                        <div className="space-y-2">
                            {workers.map(w => {
                                const health = workerHealthResults[w.worker_id];
                                return (
                                    <div key={w.worker_id} className="rounded-md border border-border p-4">
                                        <div className="flex items-start justify-between gap-4">
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-3 flex-wrap">
                                                    <span className="text-xs font-code text-text">{w.worker_id}</span>
                                                    <StatusBadge status={w.status} />
                                                    <span className="text-xs tabular-nums text-text-faint">{w.active_jobs}/{w.max_jobs} jobs</span>
                                                </div>
                                                <p className="text-xs text-text-muted mt-1 font-code">{w.address}</p>
                                                {w.mcp_disabled?.length > 0 && (
                                                    <p className="text-xs text-warning mt-1">MCP disabled: {w.mcp_disabled.join(', ')}</p>
                                                )}
                                                {w.last_heartbeat && (
                                                    <p className="text-xs text-text-faint mt-0.5">Last heartbeat: {new Date(w.last_heartbeat).toLocaleTimeString()}</p>
                                                )}
                                                {health && (
                                                    <p className={cn('text-xs mt-1', health.status === 'ok' ? 'text-success' : 'text-danger')}>
                                                        Health: {health.status} {health.latency_ms != null ? `(${health.latency_ms}ms)` : ''}
                                                        {health.data?.mcp_disabled?.length > 0 ? ` — MCP disabled: ${health.data.mcp_disabled.join(', ')}` : ''}
                                                    </p>
                                                )}
                                            </div>
                                            <div className="flex items-center gap-1 shrink-0">
                                                <Button variant="secondary" size="sm" onClick={() => checkWorkerHealth(w)}>
                                                    <Activity className="size-3.5" aria-hidden /> Health
                                                </Button>
                                                <IconButton label="Remove worker" icon={Trash2} size="sm" className="hover:text-danger" onClick={() => removeWorker(w.worker_id)} />
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}

                    {/* No Register: workers write their own row on each heartbeat, and
                        the address form posted to a route that never existed (F58). */}
                    <div className="flex justify-end">
                        <IconButton label="Refresh workers" icon={RefreshCw} size="sm" onClick={loadWorkers} />
                    </div>
                </div>
            </Section>

            <Section variant="plain" title="Observability" defaultOpen={expandedConfigs.observability}
                description="Optional OpenTelemetry export to your own collector, off by default. Nothing is sent to us."
            >
                <div className={`${FORM} space-y-4`}>
                    <Field label="OTLP endpoint" hint="Empty keeps telemetry off. Set, each request, run, step, model call and tool call becomes a span sent to this collector, across the API and its workers: ids, timings, token counts and cost — never prompts, answers or tool data. Applies on restart, and needs the telemetry packages (pip install 'synapse-orch-ai[telemetry]').">
                        <Input type="text" placeholder="http://jaeger:4317" className="font-code" {...field('otlp_endpoint')} />
                    </Field>
                    <div className="max-w-xs">
                        <EnvValue label="Metrics token" value={config.metrics_token_set ? 'Set' : 'Not set'}
                            envVar={config.env_only?.metrics_token}
                            hint={<>The API serves <span className="font-code text-text-muted">GET /api/v2/metrics</span> with it as a Bearer token.</>} />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                        <EnvValue label="Max global queue depth" value={String(config.max_global_queue_depth)}
                            envVar={config.env_only?.max_global_queue_depth}
                            hint="Returns 503 when exceeded (backpressure)." />
                        <EnvValue label="Rate limit (req/s per tenant)" value={String(config.rate_limit_per_tenant_rps)}
                            envVar={config.env_only?.rate_limit_per_tenant_rps}
                            hint="Returns 429 when exceeded." />
                    </div>
                </div>
            </Section>

            {/* Save */}
            <div className={`${FORM} space-y-2`}>
                <SaveBar onSave={handleSave} isSaving={isSaving} label="Save scale config" />
                {saveMsg && <p className="text-right text-xs text-text-muted">{saveMsg}</p>}
            </div>
        </div>
    );
}
