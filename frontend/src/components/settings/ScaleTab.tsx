"use client";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useState, useEffect, useCallback } from 'react';
import {
    CheckCircle2, XCircle, RefreshCw, Trash2, AlertTriangle,
    Activity, Server, Database, Zap, Eye, Cloud,
    ChevronDown, ChevronRight, Search, Copy, BarChart3, BookOpen, X,
} from 'lucide-react';

import { Button, Field, Input, Label, Spinner } from '@/components/ui';
import { usd } from '@/lib/money';

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

function StatusBadge({ status }: { status: string }) {
    const colors: Record<string, string> = {
        online: 'text-success bg-success/10',
        offline: 'text-danger bg-danger/10',
        draining: 'text-warning bg-warning/10',
        ok: 'text-success bg-success/10',
        error: 'text-danger bg-danger/10',
        completed: 'text-success bg-success/10',
        running: 'text-accent bg-accent/10',
        failed: 'text-danger bg-danger/10',
        paused: 'text-warning bg-warning/10',
        cancelled: 'text-text-muted bg-surface-2',
    };
    return (
        <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-2xs font-bold uppercase tracking-wider ${colors[status] || 'text-text-muted bg-surface-2'}`}>
            <span className="w-1.5 h-1.5 rounded-full bg-current" />
            {status}
        </span>
    );
}

function CollapsibleSection({
    sectionKey, title, icon: Icon, subtitle, expanded, onToggle, children,
}: {
    sectionKey: string; title: string; icon: any; subtitle: string;
    expanded: boolean; onToggle: () => void; children: React.ReactNode;
}) {
    return (
        <div className="border border-border">
            <button
                onClick={onToggle}
                className="w-full flex items-center justify-between p-4 hover:bg-surface/50 transition-colors text-left"
            >
                <div className="flex items-center gap-3">
                    <div className="p-1.5 bg-surface border border-border shrink-0 rounded-md">
                        <Icon className="h-3.5 w-3.5 text-text-muted" />
                    </div>
                    <div>
                        <Label size="sm" className="text-text">{title}</Label>
                        <p className="text-2xs text-text-faint mt-0.5">{subtitle}</p>
                    </div>
                </div>
                {expanded
                    ? <ChevronDown className="h-4 w-4 text-text-faint shrink-0" />
                    : <ChevronRight className="h-4 w-4 text-text-faint shrink-0" />
                }
            </button>
            {expanded && (
                <div className="px-6 pb-6 pt-2 border-t border-border">
                    {children}
                </div>
            )}
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
            Set by <span className="font-mono text-text-muted">{envVar ?? 'the environment'}</span> in the
            environment; change it there and restart. {hint}
        </>}>
            <Input readOnly value={value} className="font-mono bg-surface-2 text-text-muted cursor-default" />
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
            <pre className="bg-bg border border-border p-3 text-xs text-text-muted overflow-x-auto font-code leading-relaxed whitespace-pre-wrap break-words rounded-md">
                {code}
            </pre>
            <button
                onClick={copy}
                className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition-opacity px-2 py-1 bg-surface hover:bg-surface-2 text-text-muted hover:text-text text-2xs font-bold border border-border-strong rounded-md"
            >
                {copied ? '✓ Copied' : 'Copy'}
            </button>
        </div>
    );
};

const DocSection = ({ title, children, defaultOpen = false }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) => {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div className="border border-border">
            <button
                onClick={() => setOpen(o => !o)}
                className="w-full flex items-center justify-between px-4 py-3 bg-surface hover:bg-surface-2 transition-colors text-left"
            >
                <Label size="sm" className="text-text">{title}</Label>
                {open ? <ChevronDown className="w-3.5 h-3.5 text-text-faint" /> : <ChevronRight className="w-3.5 h-3.5 text-text-faint" />}
            </button>
            {open && <div className="p-4 space-y-4 border-t border-border">{children}</div>}
        </div>
    );
};

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
                aria-label="Scale Deployment Guide"
                inert={!open}
                className={`fixed top-0 right-0 z-50 h-full w-full md:w-3/4 bg-bg border-l border-border flex flex-col shadow-2xl transition-transform duration-300 ease-in-out ${open ? 'translate-x-0' : 'translate-x-full'}`}
            >
                {/* Header */}
                <div className="flex items-center justify-between px-6 py-4 border-b border-border shrink-0">
                    <div className="flex items-center gap-3">
                        <BookOpen className="w-4 h-4 text-text-muted" />
                        <h2 className="text-sm uppercase font-bold text-text tracking-wider">Scale Deployment Guide</h2>
                    </div>
                    <button onClick={onClose} aria-label="Close the deployment guide" className="text-text-faint hover:text-text transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>

                {/* Content */}
                <div className="flex-1 overflow-y-auto p-6 space-y-3 modern-scrollbar">

                    {/* Intro banner */}
                    <div className="flex items-start gap-3 px-4 py-3 bg-accent-subtle border border-accent/40 text-xs text-accent rounded-md">
                        <Zap className="w-3.5 h-3.5 text-accent shrink-0 mt-0.5" />
                        <span>
                            Scale mode turns Synapse into a <strong className="text-accent">distributed job queue</strong>. Your main Synapse instance manages definitions and the UI — workers pull jobs from Redis and execute them in parallel. All you need to get started is a <strong className="text-accent">Redis server</strong>.
                        </span>
                    </div>

                    <DocSection title="Architecture Overview" defaultOpen>
                        <p className="text-xs text-text-faint leading-relaxed">
                            In scale mode, Synapse splits into three roles. You always run exactly one <strong className="text-text">main instance</strong> (the combined image you already have). Workers and API servers are optional replicas.
                        </p>
                        <div className="space-y-2 text-xs font-mono">
                            <div className="border border-border p-3 bg-surface/50 rounded-md">
                                <p className="text-text font-bold mb-1">synapseorchai/synapse-ai  <span className="text-text-faint font-normal">(your existing install)</span></p>
                                <p className="text-text-faint">→ Hosts the UI and settings</p>
                                <p className="text-text-faint">→ Manages orchestrations, agents, tools</p>
                                <p className="text-text-faint">→ Enqueues jobs to Redis when V2 API is called</p>
                                <p className="text-text-faint">→ Streams SSE events back to clients from Redis</p>
                            </div>
                            <div className="border border-border p-3 bg-surface/50 rounded-md">
                                <p className="text-text font-bold mb-1">synapseorchai/synapse-ai-worker  <span className="text-text-faint font-normal">(scale out ×N)</span></p>
                                <p className="text-text-faint">→ Pulls jobs from Redis ARQ queue</p>
                                <p className="text-text-faint">→ Reads definitions from Postgres</p>
                                <p className="text-text-faint">→ Executes orchestrations and agent chat</p>
                                <p className="text-text-faint">→ Publishes step events back to Redis Streams</p>
                                <p className="text-text-faint">→ Health endpoint on :9000/health</p>
                            </div>
                            <div className="border border-border p-3 bg-surface/50 rounded-md">
                                <p className="text-text font-bold mb-1">synapseorchai/synapse-ai-api-server  <span className="text-text-faint font-normal">(optional — scale API tier)</span></p>
                                <p className="text-text-faint">→ Same API as the main instance but no UI</p>
                                <p className="text-text-faint">→ Useful when you want to scale API replicas separately</p>
                                <p className="text-text-faint">→ Put behind a load balancer alongside the main instance</p>
                            </div>
                        </div>
                    </DocSection>

                    <DocSection title="What You Need" defaultOpen>
                        <div className="space-y-3 text-xs">
                            <div className="flex items-start gap-3 p-3 border border-border">
                                <span className="text-success font-bold shrink-0 mt-0.5">✓ Required</span>
                                <div>
                                    <p className="text-text font-bold">Redis</p>
                                    <p className="text-text-faint mt-0.5">Job queue, cancellation signals, SSE event streams. Any Redis 6+ works — managed (Upstash, Redis Cloud, ElastiCache) or self-hosted.</p>
                                    <CodeBlock code={`# Quickest self-hosted option:\ndocker run -d -p 6379:6379 redis:7-alpine`} />
                                </div>
                            </div>
                            <div className="flex items-start gap-3 p-3 border border-border">
                                <span className="text-success font-bold shrink-0 mt-0.5">✓ Required</span>
                                <div>
                                    <p className="text-text font-bold">Postgres</p>
                                    <p className="text-text-faint mt-0.5">Orchestrations, agents and tools live here, and workers write run state here. This instance reads and writes the same database, so there is nothing to push. Any Postgres 14+ works.</p>
                                </div>
                            </div>
                            <div className="flex items-start gap-3 p-3 border border-border">
                                <span className="text-text-faint font-bold shrink-0 mt-0.5">○ Optional</span>
                                <div>
                                    <p className="text-text font-bold">S3 (or compatible)</p>
                                    <p className="text-text-faint mt-0.5">When configured, the vault, tool outputs and run logs are stored in S3, and every worker reads the same files. Without it, each process uses its own disk (<span className="font-mono">SYNAPSE_BLOB_DIR</span>) — fine on one machine; across several hosts that directory has to be shared.</p>
                                </div>
                            </div>
                        </div>
                    </DocSection>

                    <DocSection title="Quick Start — Docker Run" defaultOpen>
                        <p className="text-xs text-text-faint">The fastest way to add a worker to your existing Synapse install. Set the two required env vars and pull the image.</p>
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
                        <p className="text-xs text-text-faint">Scale horizontally by running this command on multiple machines — all workers share the same Redis queue.</p>
                    </DocSection>

                    <DocSection title="Quick Start — Docker Compose">
                        <p className="text-xs text-text-faint">Add workers alongside your existing stack using the built-in scale profile.</p>
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

                    <DocSection title="Kubernetes Deployment">
                        <p className="text-xs text-text-faint">Pre-built manifests are in <code className="font-code text-text-muted">infra/k8s/</code> in the repo. Apply them after creating a <code className="font-code text-text-muted">synapse-secrets</code> Secret.</p>
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
                        <p className="text-xs text-text-faint">The worker manifest uses <code className="font-code text-text-muted">synapseorchai/synapse-ai-worker:latest</code> with <code className="font-code text-text-muted">imagePullPolicy: Always</code> so new versions roll out automatically on pod restart.</p>
                    </DocSection>

                    <DocSection title="Environment Variables Reference">
                        <p className="text-xs text-text-faint mb-3">All variables supported by the worker image. Pass via <code className="font-code text-text-muted">-e</code>, <code className="font-code text-text-muted">--env-file</code>, or k8s Secret/ConfigMap.</p>
                        <div className="space-y-4">
                            <div>
                                <Label className="mb-2 block">Required</Label>
                                <div className="space-y-1 text-xs font-mono">
                                    {[
                                        ['REDIS_URL', 'redis://host:6379/0  or  redis+cluster://h1:6379,h2:6379'],
                                        ['SCALE_POSTGRES_URL', 'postgresql://user:pass@host:5432/dbname'],
                                    ].map(([k, v]) => (
                                        <div key={k} className="flex gap-3 py-1.5 border-b border-border/50">
                                            <span className="text-text shrink-0 w-44">{k}</span>
                                            <span className="text-text-faint">{v}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                            <div>
                                <Label className="mb-2 block">Worker Tuning</Label>
                                <div className="space-y-1 text-xs font-mono">
                                    {[
                                        ['WORKER_CONCURRENCY', '10 — max parallel jobs per worker'],
                                        ['WORKER_JOB_TIMEOUT', '3600 — seconds before a job times out'],
                                        ['WORKER_MAX_RETRIES', '3 — tries a job gets when a stopping worker cuts it off; each resumes from its checkpoint, and after the last the run is failed and dead-lettered'],
                                        ['WORKER_HEALTH_PORT', '9000 — HTTP health endpoint port'],
                                    ].map(([k, v]) => (
                                        <div key={k} className="flex gap-3 py-1.5 border-b border-border/50">
                                            <span className="text-text shrink-0 w-44">{k}</span>
                                            <span className="text-text-faint">{v}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                            <div>
                                <Label className="mb-2 block">S3 Storage (optional)</Label>
                                <div className="space-y-1 text-xs font-mono">
                                    {[
                                        ['S3_BUCKET', 'my-synapse-bucket'],
                                        ['S3_REGION', 'us-east-1'],
                                        ['S3_PREFIX', 'synapse — every key is stored under <prefix>/<tenant>/'],
                                        ['S3_ACCESS_KEY_ID', 'AKIA... (blank = use IAM role)'],
                                        ['S3_SECRET_ACCESS_KEY', ''],
                                        ['S3_ENDPOINT_URL', 'https://... (MinIO / R2 / etc.)'],
                                    ].map(([k, v]) => (
                                        <div key={k} className="flex gap-3 py-1.5 border-b border-border/50">
                                            <span className="text-text shrink-0 w-44">{k}</span>
                                            <span className="text-text-faint">{v}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                            <div>
                                <Label className="mb-2 block">Observability (optional)</Label>
                                <div className="space-y-1 text-xs font-mono">
                                    {[
                                        ['OTLP_ENDPOINT', 'your collector — telemetry is off until set'],
                                        ['SYNAPSE_TELEMETRY', 'otlp: send to OTEL_EXPORTER_OTLP_ENDPOINT'],
                                        ['METRICS_TOKEN', 'bearer token for /api/v2/metrics'],
                                        ['K8S_MODE', '1 when running on Kubernetes'],
                                        ['PGBOUNCER_MODE', '1 when Postgres URL points to PgBouncer'],
                                    ].map(([k, v]) => (
                                        <div key={k} className="flex gap-3 py-1.5 border-b border-border/50">
                                            <span className="text-text shrink-0 w-44">{k}</span>
                                            <span className="text-text-faint">{v}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        </div>
                    </DocSection>

                    <DocSection title="Setup Checklist">
                        <div className="space-y-2 text-xs">
                            {[
                                ['1', 'Connect Redis URL in the Redis Connection section above and click Test'],
                                ['2', 'Enable Scale Mode toggle'],
                                ['3', 'Connect Postgres URL in the Postgres section and click Test'],
                                ['4', 'Pull and start the worker image (Docker Run or Docker Compose above)'],
                                ['5', 'Confirm the worker appears in the Workers panel (it self-registers on startup)'],
                                ['6', 'Trigger a V2 run via API and watch the Live Queue counter increment → decrement'],
                            ].map(([n, text]) => (
                                <div key={n} className="flex items-start gap-3 py-2 border-b border-border/50 last:border-b-0">
                                    <span className="w-5 h-5 rounded-full border border-border-strong text-text-faint text-2xs font-bold flex items-center justify-center shrink-0">{n}</span>
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

    const toggle = (key: keyof ScaleConfig) => ({
        checked: Boolean(config[key]),
        onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
            setConfig(prev => ({ ...prev, [key]: e.target.checked })),
    });

    // ---------------------------------------------------------------------------
    // Render
    // ---------------------------------------------------------------------------

    return (
        <div className="space-y-6">

            {/* Docs Drawer */}
            <ScaleDocsDrawer open={docsOpen} onClose={() => setDocsOpen(false)} />

            {/* ── Analytics Header ─────────────────────────────────────────── */}
            <div>
                <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                        <BarChart3 className="h-4 w-4 text-text-faint" />
                        <h2 className="text-xs font-bold uppercase tracking-wider text-text-faint">Scale Analytics</h2>
                        {analytics && !analytics.available && (
                            <span className="text-2xs text-text-faint">(requires Postgres)</span>
                        )}
                    </div>
                    <div className="flex items-center gap-3">
                        <button
                            onClick={() => setDocsOpen(true)}
                            className="flex items-center gap-1.5 px-3 py-1.5 text-2xs uppercase font-bold tracking-wider text-text-muted hover:text-text bg-surface border border-border hover:border-text-faint transition-colors rounded-md"
                        >
                            <BookOpen className="h-3 w-3" />
                            Deploy Docs
                        </button>
                        <button
                            onClick={() => { loadAnalytics(); loadRecentRuns(); loadQueueStats(); }}
                            className="flex items-center gap-1.5 text-2xs text-text-faint hover:text-text transition-colors uppercase tracking-wider"
                        >
                            <RefreshCw className="h-3 w-3" /> Refresh
                        </button>
                    </div>
                </div>

                <div className="grid grid-cols-4 gap-3 mb-3">
                    {[
                        { label: 'Total Runs', value: analytics?.available ? analytics.total_runs : '—', color: 'text-text' },
                        { label: 'Runs Today', value: analytics?.available ? analytics.runs_today : '—', color: 'text-text' },
                        { label: 'Active Now', value: analytics?.available ? (analytics.status_counts?.running ?? 0) : (queueStats?.active ?? '—'), color: 'text-accent' },
                        { label: 'Success Rate', value: analytics?.available ? `${analytics.success_rate}%` : '—', color: (analytics?.success_rate ?? 0) >= 90 ? 'text-success' : 'text-warning' },
                    ].map(stat => (
                        <div key={stat.label} className="border border-border p-4 text-center bg-bg rounded-md">
                            <p className={`text-2xl font-bold font-mono ${stat.color}`}>{stat.value}</p>
                            <Label className="mt-1 block">{stat.label}</Label>
                        </div>
                    ))}
                </div>

                <div className="grid grid-cols-4 gap-3">
                    {[
                        { label: 'Workers Online', value: analytics?.available ? analytics.workers_online : (workers.filter(w => w.status === 'online').length || '—'), color: 'text-success' },
                        { label: 'Avg Cost / Run', value: analytics?.available ? usd(analytics.avg_cost_usd) : '—', color: 'text-text' },
                        { label: 'Cost Today', value: analytics?.available ? usd(analytics.total_cost_usd_today) : '—', color: 'text-text' },
                        { label: 'Cache Hit Rate', value: analytics?.available ? `${analytics.cache_hit_rate}%` : '—', color: 'text-accent' },
                    ].map(stat => (
                        <div key={stat.label} className="border border-border p-4 text-center bg-bg rounded-md">
                            <p className={`text-2xl font-bold font-mono ${stat.color}`}>{stat.value}</p>
                            <Label className="mt-1 block">{stat.label}</Label>
                        </div>
                    ))}
                </div>
            </div>

            {/* ── Two-column monitoring section ────────────────────────────── */}
            <div className="grid gap-6" style={{ gridTemplateColumns: '70% 30%' }}>

                {/* Left: Search + Recent Runs */}
                <div className="space-y-4 min-w-0">

                    {/* Search bar */}
                    <div className="border border-border p-4">
                        <Label htmlFor="scale-run-search" className="mb-3 block">Search by Run ID or Session ID</Label>
                        <div className="flex gap-2">
                            <div className="relative flex-1">
                                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-text-faint" />
                                <Input
                                    id="scale-run-search"
                                    type="text"
                                    placeholder="run_abc_1234567890 or sess_xyz..."
                                    className="w-full bg-surface border border-border-strong text-text text-sm pl-8 pr-3 py-2 font-mono focus:outline-none focus:border-text-faint rounded-md"
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
                        <p className="text-2xs text-text-faint mt-2">Prefix <span className="font-mono text-text-muted">sess_</span> to search by session ID and see all runs in that session.</p>

                        {/* Search error */}
                        {searchError && (
                            <p className="text-xs text-danger mt-3 flex items-center gap-1.5">
                                <XCircle className="h-3 w-3" /> {searchError}
                            </p>
                        )}

                        {/* Run detail result */}
                        {searchRunDetail && (
                            <div className="mt-4 border border-border-strong bg-bg p-4 rounded-md">
                                <div className="flex items-start justify-between gap-4 mb-3">
                                    <div className="flex items-center gap-2 flex-wrap min-w-0">
                                        <button
                                            onClick={() => copyToClipboard(searchRunDetail.run_id)}
                                            className="font-mono text-xs text-text-muted hover:text-text flex items-center gap-1 truncate"
                                        >
                                            {searchRunDetail.run_id}
                                            <Copy className="h-3 w-3 shrink-0" />
                                        </button>
                                        <StatusBadge status={searchRunDetail.status} />
                                    </div>
                                    <button onClick={() => setSearchRunDetail(null)} className="text-text-faint hover:text-text-muted text-xs shrink-0">✕</button>
                                </div>
                                <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs">
                                    <div className="flex justify-between">
                                        <span className="text-text-faint">Orchestration</span>
                                        <span className="font-mono text-text truncate ml-2">{searchRunDetail.orchestration_id || '—'}</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-text-faint">Worker</span>
                                        <span className="font-mono text-text truncate ml-2">{searchRunDetail.worker_id || '—'}</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-text-faint">Duration</span>
                                        <span className="text-text">{formatDuration(searchRunDetail.started_at, searchRunDetail.ended_at)}</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-text-faint">Cost</span>
                                        <span className="text-text">{usd(searchRunDetail.total_cost_usd)}</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-text-faint">Tokens</span>
                                        <span className="text-text">{searchRunDetail.total_tokens_used?.toLocaleString() ?? '—'}</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-text-faint">Cache Hits</span>
                                        <span className="text-text">{searchRunDetail.cache_hit_count ?? '—'}</span>
                                    </div>
                                    {searchRunDetail.current_step_id && (
                                        <div className="flex justify-between col-span-2">
                                            <span className="text-text-faint">Current Step</span>
                                            <span className="font-mono text-text">{searchRunDetail.current_step_id}</span>
                                        </div>
                                    )}
                                    {searchRunDetail.waiting_for_human && (
                                        <div className="col-span-2 flex items-start gap-2 p-2 bg-warning-subtle border border-warning/40 text-warning rounded-md">
                                            <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
                                            <span className="text-2xs">Waiting for human input{searchRunDetail.human_prompt ? `: ${searchRunDetail.human_prompt}` : ''}</span>
                                        </div>
                                    )}
                                    {searchRunDetail.estimated_savings_usd != null && searchRunDetail.estimated_savings_usd > 0 && (
                                        <div className="flex justify-between">
                                            <span className="text-text-faint">Cache Savings</span>
                                            <span className="text-success">{usd(searchRunDetail.estimated_savings_usd)}</span>
                                        </div>
                                    )}
                                </div>
                                <div className="mt-2 text-2xs text-text-faint">
                                    Started: {searchRunDetail.started_at ? new Date(searchRunDetail.started_at).toLocaleString() : '—'}
                                    {searchRunDetail.ended_at && ` · Ended: ${new Date(searchRunDetail.ended_at).toLocaleString()}`}
                                </div>
                            </div>
                        )}

                        {/* Session search results */}
                        {searchSessionRuns !== null && (
                            <div className="mt-4">
                                <Label className="mb-2 block">
                                    {searchSessionRuns.length} run{searchSessionRuns.length !== 1 ? 's' : ''} in session
                                </Label>
                                {searchSessionRuns.length === 0 ? (
                                    <p className="text-xs text-text-faint text-center py-4">No runs found for this session.</p>
                                ) : (
                                    <div className="space-y-1">
                                        {searchSessionRuns.map(run => (
                                            // A button, so a keyboard opens a run as a click does (F236).
                                            <button
                                                type="button"
                                                key={run.run_id}
                                                className="flex w-full items-center justify-between border border-border p-2.5 text-left hover:bg-surface/50 transition-colors rounded-md"
                                                onClick={() => { setSearchQuery(run.run_id); handleSearch(run.run_id); }}
                                            >
                                                <span className="flex items-center gap-2 min-w-0">
                                                    <span className="font-mono text-2xs text-text-muted truncate">{run.run_id}</span>
                                                    <StatusBadge status={run.status} />
                                                </span>
                                                <span className="flex items-center gap-3 text-2xs text-text-faint shrink-0">
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

                    {/* Recent Runs table */}
                    <div className="border border-border p-4">
                        <div className="flex items-center justify-between mb-3">
                            <Label>Recent Runs</Label>
                            <button
                                onClick={loadRecentRuns}
                                className="flex items-center gap-1 text-2xs text-text-faint hover:text-text transition-colors"
                            >
                                <RefreshCw className="h-3 w-3" /> Refresh
                            </button>
                        </div>

                        {recentRuns.length === 0 ? (
                            <div className="text-center py-8 border border-border border-dashed">
                                <Activity className="h-6 w-6 text-text-faint mx-auto mb-2" />
                                <p className="text-sm text-text-faint">No runs recorded yet.</p>
                                <p className="text-xs text-text-faint mt-1">Runs appear here when scale mode is active and Postgres is connected.</p>
                            </div>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-xs">
                                    <thead>
                                        <tr className="border-b border-border">
                                            {['Run ID', 'Status', 'Orchestration', 'Started', 'Duration', 'Cost'].map(h => (
                                                <th key={h} className="text-left text-2xs font-bold uppercase tracking-wider text-text-faint pb-2 pr-4">{h}</th>
                                            ))}
                                        </tr>
                                    </thead>
                                    <tbody className="divide-y divide-border/50">
                                        {recentRuns.map(run => (
                                            <tr
                                                key={run.run_id}
                                                className="hover:bg-surface/50 cursor-pointer transition-colors"
                                                onClick={() => { setSearchQuery(run.run_id); handleSearch(run.run_id); }}
                                            >
                                                <td className="py-2 pr-4">
                                                    <div className="flex items-center gap-1.5">
                                                        {/* The row opens on a click; its id does the same for a keyboard (F236). */}
                                                        <button
                                                            type="button"
                                                            onClick={e => { e.stopPropagation(); setSearchQuery(run.run_id); handleSearch(run.run_id); }}
                                                            className="font-mono text-text truncate max-w-[140px] text-left rounded-sm"
                                                            title={run.run_id}
                                                            aria-label={`Open run ${run.run_id}`}
                                                        >
                                                            {run.run_id.length > 20 ? `${run.run_id.slice(0, 20)}…` : run.run_id}
                                                        </button>
                                                        <button
                                                            type="button"
                                                            onClick={e => { e.stopPropagation(); copyToClipboard(run.run_id); }}
                                                            className="text-text-faint hover:text-text-muted shrink-0"
                                                            aria-label={`Copy run id ${run.run_id}`}
                                                            title="Copy run id"
                                                        >
                                                            <Copy className="h-3 w-3" aria-hidden />
                                                        </button>
                                                    </div>
                                                </td>
                                                <td className="py-2 pr-4"><StatusBadge status={run.status} /></td>
                                                <td className="py-2 pr-4">
                                                    <span className="font-mono text-text-muted truncate max-w-[100px] block" title={run.orchestration_id}>
                                                        {run.orchestration_id ? run.orchestration_id.slice(0, 14) : '—'}
                                                    </span>
                                                </td>
                                                <td className="py-2 pr-4 text-text-faint whitespace-nowrap">
                                                    {run.started_at ? new Date(run.started_at).toLocaleTimeString() : '—'}
                                                </td>
                                                <td className="py-2 pr-4 text-text-muted whitespace-nowrap">
                                                    {formatDuration(run.started_at, run.ended_at)}
                                                </td>
                                                <td className="py-2 text-text-muted">{usd(run.total_cost_usd)}</td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </div>
                </div>

                {/* Right: Queue + Workers + DLQ */}
                <div className="space-y-4 min-w-0">

                    {/* Live Queue */}
                    <div className="border border-border p-4">
                        <Label className="mb-3 block">Live Queue</Label>
                        <div className="grid grid-cols-2 gap-2">
                            {[
                                { label: 'Queued', value: queueStats?.queued ?? '—', color: 'text-text' },
                                { label: 'Active', value: queueStats?.active ?? '—', color: 'text-accent' },
                                { label: 'Failed', value: queueStats?.failed ?? '—', color: 'text-danger' },
                                { label: 'DLQ', value: queueStats?.dlq_count ?? '—', color: 'text-warning' },
                            ].map(stat => (
                                <div key={stat.label} className="border border-border p-3 text-center bg-bg rounded-md">
                                    <p className={`text-xl font-bold font-mono ${stat.color}`}>{stat.value}</p>
                                    <Label className="mt-0.5 block">{stat.label}</Label>
                                </div>
                            ))}
                        </div>
                        {queueStats && !queueStats.available && (
                            <p className="text-2xs text-text-faint mt-2 text-center">Redis not connected</p>
                        )}
                    </div>

                    {/* Workers mini */}
                    <div className="border border-border p-4">
                        <div className="flex items-center justify-between mb-3">
                            <Label>Workers</Label>
                            <button onClick={loadWorkers} aria-label="Refresh workers" title="Refresh workers" className="text-text-faint hover:text-text-muted transition-colors">
                                <RefreshCw className="h-3 w-3" />
                            </button>
                        </div>
                        {workers.length === 0 ? (
                            <p className="text-xs text-text-faint text-center py-3">No workers registered</p>
                        ) : (
                            <div className="space-y-2">
                                {workers.map(w => (
                                    <div key={w.worker_id} className="flex items-center justify-between gap-2 border border-border p-2">
                                        <div className="min-w-0">
                                            <div className="flex items-center gap-1.5 flex-wrap">
                                                <StatusBadge status={w.status} />
                                                <span className="text-2xs font-mono text-text-faint truncate">{w.hostname || w.worker_id.slice(0, 16)}</span>
                                            </div>
                                            <p className="text-2xs text-text-faint mt-0.5">{w.active_jobs}/{w.max_jobs} jobs</p>
                                        </div>
                                        <button
                                            onClick={() => checkWorkerHealth(w)}
                                            className="text-2xs text-text-faint hover:text-text border border-border-strong px-2 py-1 shrink-0 transition-colors"
                                        >
                                            <Activity className="h-3 w-3" />
                                        </button>
                                    </div>
                                ))}
                            </div>
                        )}
                        <button
                            onClick={() => toggleSection('workers')}
                            className="mt-3 text-2xs text-text-faint hover:text-text-muted transition-colors w-full text-center"
                        >
                            Manage workers ↓
                        </button>
                    </div>

                    {/* DLQ mini */}
                    <div className="border border-border p-4">
                        <div className="flex items-center justify-between mb-3">
                            <Label>Failed Jobs (DLQ)</Label>
                            <button onClick={loadDlq} aria-label="Refresh the failed jobs list" className="text-text-faint hover:text-text-muted transition-colors">
                                {dlqLoading ? <Spinner size={12} /> : <RefreshCw className="h-3 w-3" />}
                            </button>
                        </div>
                        {dlqEntries.length === 0 ? (
                            <div className="flex items-center gap-2 text-xs text-text-faint justify-center py-3">
                                <CheckCircle2 className="h-3.5 w-3.5 text-success" /> No failed jobs
                            </div>
                        ) : (
                            <div className="space-y-2">
                                {dlqEntries.slice(0, 3).map(entry => (
                                    <div key={entry.id} className="border border-border p-2">
                                        <div className="flex items-start justify-between gap-2">
                                            <div className="min-w-0">
                                                <p className="font-mono text-2xs text-text-muted truncate">{entry.run_id || entry.id}</p>
                                                <p className="text-2xs text-danger truncate mt-0.5">{entry.error_message}</p>
                                                <p className="text-2xs text-text-faint">{entry.attempt_count} attempts</p>
                                                {dlqRefused?.id === entry.id && (
                                                    <p role="alert" className="text-2xs text-warning mt-1">{dlqRefused.message}</p>
                                                )}
                                            </div>
                                            <button
                                                onClick={() => retryDlq(entry.id)}
                                                disabled={retryingDlq === entry.id}
                                                aria-label={`Retry ${entry.run_id || entry.id}`}
                                                title="Resume this run from the step it failed at"
                                                className="text-2xs border border-border-strong px-2 py-1 text-text-faint hover:text-text disabled:opacity-40 shrink-0 transition-colors"
                                            >
                                                {retryingDlq === entry.id ? <Spinner size={12} /> : <RefreshCw className="h-3 w-3" />}
                                            </button>
                                        </div>
                                    </div>
                                ))}
                                {dlqEntries.length > 3 && (
                                    <p className="text-2xs text-text-faint text-center">+{dlqEntries.length - 3} more — expand Queue section below</p>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            </div>

            {/* ── Configuration Sections (collapsed by default) ────────────── */}

            <CollapsibleSection sectionKey="redis" title="Redis Connection" icon={Zap}
                subtitle="Required for distributed workers, queue management, and distributed cancellation."
                expanded={expandedConfigs.redis} onToggle={() => toggleSection('redis')}
            >
                <div className="space-y-4 mt-4">
                    <div>
                        <div className="flex items-end gap-2">
                            <Field label="Redis URL" className="flex-1">
                                <Input type="text" placeholder="redis://localhost:6379/0"
                                    className="font-mono"
                                    {...field('redis_url')} />
                            </Field>
                            <Button variant="secondary" onClick={testRedis}
                                loading={redisTesting} disabled={!config.redis_url}>
                                Test
                            </Button>
                        </div>
                        {redisStatus && (
                            <p className={`text-xs mt-1.5 flex items-center gap-1.5 ${redisStatus.ok ? 'text-success' : 'text-danger'}`}>
                                {redisStatus.ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                                {redisStatus.msg}
                            </p>
                        )}
                        <p className="text-2xs text-text-faint mt-1.5">For Redis Cluster: <span className="font-mono">redis+cluster://host1:6379,host2:6379</span></p>
                    </div>

                    <div className="flex items-center justify-between py-3 border-t border-border">
                        <div>
                            <p className="text-sm text-text font-medium">Enable Scale Mode</p>
                            <p className="text-xs text-text-faint mt-0.5">Route V2 API calls through Redis ARQ workers instead of in-process execution.</p>
                        </div>
                        <label className="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" role="switch" aria-label="Enable scale mode" className="sr-only peer" {...toggle('scale_mode_enabled')} />
                            <div className="w-11 h-6 bg-surface-2 rounded-full peer peer-checked:bg-accent transition-colors" />
                            <div className="absolute left-1 top-1 bg-text peer-checked:bg-accent-fg w-4 h-4 rounded-full transition-all peer-checked:translate-x-5" />
                        </label>
                    </div>

                    <div className="grid grid-cols-2 gap-4 pt-2">
                        <EnvValue label="Worker Concurrency" value={String(config.worker_concurrency)}
                            envVar={config.env_only?.worker_concurrency}
                            hint="Max parallel jobs per worker process." />
                    </div>
                </div>
            </CollapsibleSection>

            <CollapsibleSection sectionKey="postgres" title="Postgres" icon={Database}
                subtitle="Workers read orchestrations, agents and tools from the same database this instance writes."
                expanded={expandedConfigs.postgres} onToggle={() => toggleSection('postgres')}
            >
                <div className="space-y-4 mt-4">
                    <div>
                        <div className="flex items-end gap-2">
                            <Field label="Postgres URL (Scale)" className="flex-1">
                                <Input type="text" placeholder="postgresql://user:pass@host:5432/synapse"
                                    className="font-mono"
                                    {...field('scale_postgres_url')} />
                            </Field>
                            <Button variant="secondary" onClick={testPostgres}
                                loading={pgTesting} disabled={!config.scale_postgres_url}>
                                Test
                            </Button>
                        </div>
                        {pgStatus && (
                            <p className={`text-xs mt-1.5 flex items-center gap-1.5 ${pgStatus.ok ? 'text-success' : 'text-danger'}`}>
                                {pgStatus.ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                                {pgStatus.msg}
                            </p>
                        )}
                        <p className="text-2xs text-text-faint mt-1.5">Separate from the embed-code Postgres. Behind PgBouncer, point this at the PgBouncer port.</p>
                    </div>

                    <div className="pt-3 border-t border-border max-w-xs">
                        <EnvValue label="PgBouncer Mode" value={config.pgbouncer_mode ? 'On' : 'Off'}
                            envVar={config.env_only?.pgbouncer_mode}
                            hint="Uses NullPool, which PgBouncer in transaction mode requires." />
                    </div>

                </div>
            </CollapsibleSection>

            <CollapsibleSection sectionKey="s3" title="Storage (S3)" icon={Cloud}
                subtitle="When a bucket is set, the vault, tool outputs and run logs live in S3, and every worker reads the same files."
                expanded={expandedConfigs.s3} onToggle={() => toggleSection('s3')}
            >
                <div className="space-y-4 mt-4">
                    <div className="grid grid-cols-2 gap-4">
                        <div>
                                                        <Field label="S3 Bucket">
                                <Input type="text" placeholder="my-synapse-bucket" className="font-mono" {...field('s3_bucket')} />
                            </Field>
                        </div>
                        <div>
                                                        <Field label="Region">
                                <Input type="text" placeholder="us-east-1" className="font-mono" {...field('s3_region')} />
                            </Field>
                        </div>
                    </div>
                    <div>
                                                <Field label="Key Prefix" hint="All objects are stored under this prefix.">
                            <Input type="text" placeholder="synapse" className="font-mono" {...field('s3_prefix')} />
                        </Field>
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                        <div>
                                                        <Field label="Access Key ID">
                                <Input type="text" placeholder="AKIA... (optional)" className="font-mono" {...field('s3_access_key_id')} />
                            </Field>
                        </div>
                        <div>
                                                        <Field label="Secret Access Key"
                                hint={config.s3_secret_access_key_set
                                    ? 'Stored, and never shown. Leave empty to keep it.'
                                    : undefined}>
                                {/* Write-only (F95): the route no longer reads it back,
                                    and an empty field keeps what is stored. */}
                                <Input type="password" autoComplete="new-password"
                                    placeholder={config.s3_secret_access_key_set ? '•••••••• stored' : '••••••••'}
                                    className="font-mono" {...field('s3_secret_access_key')} />
                            </Field>
                        </div>
                    </div>
                    <div>
                        <Field label="Endpoint URL" hint="Optional — for MinIO, Cloudflare R2, and other S3-compatible stores.">
                            <Input type="text" placeholder="https://account.r2.cloudflarestorage.com"
                                className="font-mono"
                                {...field('s3_endpoint_url')} />
                        </Field>
                    </div>
                    <div className="flex items-center gap-3 pt-1">
                        <Button variant="secondary" onClick={testS3}
                            loading={s3Testing} disabled={!config.s3_bucket}>
                            {!s3Testing && <Cloud className="size-3.5" aria-hidden />}
                            Test S3 Connection
                        </Button>
                        {s3Status && (
                            <p className={`text-xs flex items-center gap-1.5 ${s3Status.ok ? 'text-success' : 'text-danger'}`}>
                                {s3Status.ok ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                                {s3Status.msg}
                            </p>
                        )}
                    </div>
                    <p className="text-2xs text-text-faint">Leave the Access Key ID blank to use an IAM role or environment credentials — saving without one also clears a stored secret.</p>
                    {/* F57: the bucket became the real blob store in batch 1C. It is
                        chosen when each process starts, and reads go to it alone. */}
                    <div className="flex items-start gap-2 text-2xs text-warning border border-warning/40 bg-warning-subtle rounded-md px-3 py-2">
                        <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-px" aria-hidden />
                        <p>
                            A bucket takes effect when the server and every worker restart. Files already on
                            local disk are not copied: after the switch the vault starts empty in S3, and what
                            was there stays in <span className="font-mono">SYNAPSE_BLOB_DIR</span> until uploaded again.
                        </p>
                    </div>
                </div>
            </CollapsibleSection>

            <CollapsibleSection sectionKey="workers" title="Workers" icon={Server}
                subtitle="Remote worker instances running Dockerfile.worker. Each worker pulls jobs from Redis ARQ."
                expanded={expandedConfigs.workers} onToggle={() => toggleSection('workers')}
            >
                <div className="mt-4">
                    {workers.length === 0 ? (
                        <div className="text-center py-8 border border-border border-dashed">
                            <Server className="h-8 w-8 text-text-faint mx-auto mb-3" />
                            <p className="text-sm text-text-faint">No workers have reported in.</p>
                            <p className="text-xs text-text-faint mt-1">A worker appears here on its first heartbeat. Start one from <span className="font-mono text-text-muted">Dockerfile.worker</span>, pointed at the same Redis and database.</p>
                        </div>
                    ) : (
                        <div className="space-y-2 mb-4">
                            {workers.map(w => {
                                const health = workerHealthResults[w.worker_id];
                                return (
                                    <div key={w.worker_id} className="border border-border p-4">
                                        <div className="flex items-start justify-between gap-4">
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-2 flex-wrap">
                                                    <span className="text-xs font-mono text-text">{w.worker_id}</span>
                                                    <StatusBadge status={w.status} />
                                                    <span className="text-2xs text-text-faint">{w.active_jobs}/{w.max_jobs} jobs</span>
                                                </div>
                                                <p className="text-xs text-text-faint mt-1 font-mono">{w.address}</p>
                                                {w.mcp_disabled?.length > 0 && (
                                                    <p className="text-2xs text-warning mt-1">MCP disabled: {w.mcp_disabled.join(', ')}</p>
                                                )}
                                                {w.last_heartbeat && (
                                                    <p className="text-2xs text-text-faint mt-0.5">Last heartbeat: {new Date(w.last_heartbeat).toLocaleTimeString()}</p>
                                                )}
                                                {health && (
                                                    <p className={`text-2xs mt-1 ${health.status === 'ok' ? 'text-success' : 'text-danger'}`}>
                                                        Health: {health.status} {health.latency_ms != null ? `(${health.latency_ms}ms)` : ''}
                                                        {health.data?.mcp_disabled?.length > 0 ? ` — MCP disabled: ${health.data.mcp_disabled.join(', ')}` : ''}
                                                    </p>
                                                )}
                                            </div>
                                            <div className="flex gap-2 shrink-0">
                                                <button onClick={() => checkWorkerHealth(w)}
                                                    className="px-3 py-1.5 text-2xs font-bold uppercase tracking-wider border border-border-strong text-text-muted hover:text-text hover:border-text-faint transition-colors flex items-center gap-1">
                                                    <Activity className="h-3 w-3" /> Health
                                                </button>
                                                <button onClick={() => removeWorker(w.worker_id)} className="p-1.5 text-text-faint hover:text-danger transition-colors">
                                                    <Trash2 className="h-3.5 w-3.5" />
                                                </button>
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    )}

                    {/* No Register: workers write their own row on each heartbeat, and
                        the address form posted to a route that never existed (F58). */}
                    <div className="flex justify-end mt-4 pt-4 border-t border-border">
                        <button onClick={loadWorkers} aria-label="Refresh workers" title="Refresh workers" className="p-2 text-text-faint hover:text-text transition-colors">
                            <RefreshCw className="h-4 w-4" />
                        </button>
                    </div>
                </div>
            </CollapsibleSection>

            <CollapsibleSection sectionKey="observability" title="Observability" icon={Activity}
                subtitle="Optional OpenTelemetry export to your own collector, off by default. Nothing is sent to us."
                expanded={expandedConfigs.observability} onToggle={() => toggleSection('observability')}
            >
                <div className="space-y-4 mt-4">
                    <div>
                        <Field label="OTLP Endpoint" hint="Empty keeps telemetry off. Set, each request, run, step, model call and tool call becomes a span sent to this collector, across the API and its workers: ids, timings, token counts and cost — never prompts, answers or tool data. Applies on restart, and needs the telemetry packages (pip install 'synapse-orch-ai[telemetry]').">
                            <Input type="text" placeholder="http://jaeger:4317" className="font-mono" {...field('otlp_endpoint')} />
                        </Field>
                    </div>
                    <div className="max-w-xs">
                        <EnvValue label="Metrics Token" value={config.metrics_token_set ? 'Set' : 'Not set'}
                            envVar={config.env_only?.metrics_token}
                            hint={<>The API serves <span className="font-mono text-text-muted">GET /api/v2/metrics</span> with it as a Bearer token.</>} />
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                        <EnvValue label="Max Global Queue Depth" value={String(config.max_global_queue_depth)}
                            envVar={config.env_only?.max_global_queue_depth}
                            hint="Returns 503 when exceeded (backpressure)." />
                        <EnvValue label="Rate Limit (req/s per tenant)" value={String(config.rate_limit_per_tenant_rps)}
                            envVar={config.env_only?.rate_limit_per_tenant_rps}
                            hint="Returns 429 when exceeded." />
                    </div>
                </div>
            </CollapsibleSection>

            {/* Save Button */}
            <div className="flex items-center gap-4 pt-2">
                <button onClick={handleSave} disabled={isSaving}
                    className="flex items-center gap-2 px-6 py-2.5 text-sm font-bold uppercase tracking-wider bg-accent text-accent-fg hover:bg-accent-hover disabled:opacity-40 transition-colors rounded-md">
                    {isSaving ? <Spinner size={16} /> : null}
                    Save Scale Config
                </button>
                {saveMsg && <p className="text-xs text-text-muted">{saveMsg}</p>}
            </div>
        </div>
    );
}
