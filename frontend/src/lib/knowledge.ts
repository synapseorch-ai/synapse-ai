/*
 * Knowledge (D157): knowledge bases, their documents, and uploads — the client side of
 * `backend/core/routes/knowledge.py`, whose bodies are frozen in
 * `backend/core/knowledge/api_models.py`.
 *
 * A knowledge base is a Vault folder made searchable. Each file in it is a
 * document with a status; a document's report says what happened to it, stage
 * by stage. Uploads go in parts (at most 8 MiB each, under the proxy's body
 * limit): open a session, PUT each part, complete. Web sources (S1, D165) are
 * crawled into the folder's `_web/`; each page is a document too.
 */
import { useEffect, useState } from 'react';

import type { BadgeTone, ComboboxOption } from '@/components/ui';

/** A `[K#]` label resolved to where the words are (`core.knowledge.api_models.Citation`). */
export interface KnowledgeCitation {
    label: string;
    kb_id: string;
    doc_id: string;
    chunk_id: string;
    path: string;
    source: string;
    source_url?: string | null;
    title?: string | null;
    page_start?: number | null;
    page_end?: number | null;
    char_start: number;
    char_end: number;
    heading_path: string[];
    snippet: string;
    indexed_at?: string | null;
    snapshot_id: string;
    /** Whether the words the answer quoted for this label are in its passage. */
    quote_check?: { quote: string; matched: boolean; method: 'exact' | 'normalized' } | null;
}

export type DocStatus =
    | 'queued' | 'parsing' | 'indexing' | 'indexed' | 'partial' | 'failed' | 'skipped' | 'paused' | 'blocked'
    | 'deleting';

/** The stages a working document passes, as `DocumentProgress.stage` names them (CCR-12). */
export type ProgressStage = 'parse' | 'ocr' | 'enrich' | 'embed' | 'index';

/** A provider's rate limit being waited out (`api_models.ProgressWait`, CCR-12): the work goes on by itself. */
export type ProgressWait = { reason: 'rate_limited'; provider: string; provider_id: string; until: string | null };

/** How far a working document has got (`api_models.DocumentProgress`, CCR-12). No `total`: a stage that cannot
 *  count (a reader that takes the file in one pass, the index's one write). `eta_s` only once there is a pace. */
export type DocumentProgress = {
    stage: ProgressStage;
    done: number | null;
    total: number | null;
    unit: 'pages' | 'passages' | null;
    eta_s: number | null;
    waiting: ProgressWait | null;
    updated_at: string | null;
};

/** A rebuild of the index after a change to how it is built (`api_models.SnapshotBuild`, CCR-5): the
 *  serving index answers until the new one holds every document. CCR-12: its passages, the stage of the
 *  document being built, the time left and a rate limit's wait. */
export type SnapshotBuild = {
    snapshot_id: string;
    status: 'building' | 'failed';
    docs_total: number;
    docs_done: number;
    docs_failed: number;
    started_at: string | null;
    /** Waiting for budget (`budget_cap`), a wallet (`wallet_402`) or a key (`embedder_unavailable`). */
    paused_reason: string | null;
    error: string | null;
    passages_done?: number | null;
    passages_total?: number | null;
    stage?: ProgressStage | null;
    eta_s?: number | null;
    waiting?: ProgressWait | null;
};

export type KnowledgeBaseState = {
    counts_by_status: Partial<Record<DocStatus, number>>;
    active_snapshot: string | null;
    building_snapshot: string | null;
    month_spend_usd: number;
    paused_reason: string | null;
    build?: SnapshotBuild | null;
};

export type WebSourceKind = 'page' | 'sitemap' | 'site';

/** One URL the base keeps fetched (`kb_settings.WebSource`). */
export type WebSource = { id: string; url: string; kind: WebSourceKind; include: string[]; exclude: string[] };

/** A base's crawl settings and sources (`kb_settings.Web`). */
export type WebSettings = {
    max_depth: number;
    max_pages: number;
    rps_per_host: number;
    /** Days between re-crawls; 0 never re-crawls. */
    recrawl_days: number;
    sources: WebSource[];
};

/** How a base's documents are cut into passages (`kb_settings.Chunking`). Changing it rebuilds the index. */
export type ChunkingSettings = {
    mode: 'structure' | 'fixed' | 'page' | 'none';
    target_tokens: number;
    max_tokens: number;
    overlap_pct: number;
};

/** The embedding model; "" is the workspace's default (`kb_settings.Embedding`). Changing it rebuilds the index. */
export type EmbeddingSettings = { model: string; dims: number | null; profile: string | null };

/** `enabled` null is the deployment's default: on where a reranker is configured (`kb_settings.Rerank`). */
export type RerankSettings = { enabled: boolean | null; model: string | null; top_n: number; candidates: number };

export type FusionSettings = {
    method: 'rrf';
    rrf_k: number;
    weights: { lexical?: number; vector?: number };
    overfetch: number;
};

export type RetrievalSettings = {
    top_k: number;
    score_threshold: number | null;
    parent_expansion: 'off' | 'section' | 'neighbors';
    neighbor_window: number;
    max_context_tokens: number;
};

/** "prefix" is free; "contextual" (Contextual Retrieval, CR1) has a chat model write each passage's context while it
 *  is indexed — `contextual_model`, or the workspace's default when null. Changing either rebuilds the index. */
export type EnrichmentSettings = { mode: 'prefix' | 'contextual'; contextual_model: string | null; extract_metadata: boolean };

export type KnowledgeBase = {
    id: string;
    name: string;
    description: string;
    folder: string;
    status: 'active' | 'disabled';
    language: string;
    /** Widget and channel visitors may search it, when the tools are ticked there too (D153 #12). */
    visitor_access: boolean;
    web: WebSettings;
    /** USD a month, every Knowledge cost counted; null: no cap. */
    budget_monthly_usd: number | null;
    chunking: ChunkingSettings;
    embedding: EmbeddingSettings;
    rerank: RerankSettings;
    fusion: FusionSettings;
    retrieval: RetrievalSettings;
    enrichment: EnrichmentSettings;
    revision: number;
    created_at: string;
    updated_at: string;
    state: KnowledgeBaseState;
    /** On an edit's answer: a change to how the index is built started a rebuild. */
    reindex_started?: boolean;
};

export type KnowledgeDocument = {
    id: string;
    kb_id: string;
    path: string;
    source: 'upload' | 'web' | 'record' | 'vault';
    source_url: string | null;
    status: DocStatus;
    status_reason: string | null;
    error: string | null;
    version: number;
    sha256: string | null;
    size_bytes: number | null;
    mime: string | null;
    title: string | null;
    doc_date: string | null;
    metadata: Record<string, unknown>;
    pages: number | null;
    chunks: number | null;
    tokens: number | null;
    created_at: string | null;
    updated_at: string | null;
    indexed_at: string | null;
    /** While it is being read or indexed: how far it has got (CCR-12). */
    progress?: DocumentProgress | null;
};

export type StageReport = {
    stage: string;
    status: 'ok' | 'skipped' | 'partial' | 'failed';
    started_at: string | null;
    duration_ms: number;
    detail: Record<string, unknown>;
    error: { code: string; message: string; retryable: boolean } | null;
};

export type IngestionReport = {
    doc_id: string;
    path: string;
    attempt: number;
    started_at: string | null;
    finished_at: string | null;
    outcome: DocStatus;
    status_reason: string | null;
    stages: StageReport[];
    pages: { total: number; with_text: number; no_text_layer: number; ocr_applied: number; ocr_failed: number };
    chunks: { count: number; by_kind: Record<string, number>; tokens: number };
    warnings: { code: string; message: string; page: number | null; count: number }[];
    cost: { est_usd: number; charged_microusd: number | null; by_stage: Record<string, number> };
    parser: { name: string; version: string } | null;
    snapshot_id: string | null;
    retry: { allowed: boolean; hint: string | null };
};

export type KnowledgeDocumentDetail = KnowledgeDocument & { report: IngestionReport | null };

export type CrawlStatus = 'idle' | 'queued' | 'running' | 'done' | 'capped' | 'failed' | 'cancelled' | 'interrupted';

/** A web source and its latest crawl (`api_models.WebCrawlState`, CCR-4). */
export type WebCrawlState = {
    source_id: string;
    url: string;
    kind: WebSourceKind;
    status: CrawlStatus;
    crawl_id: string | null;
    started_at: string | null;
    finished_at: string | null;
    /** The pages this source holds now. */
    pages: number;
    counts: {
        fetched: number; unchanged: number; written: number; deleted: number; blocked: number; failed: number;
        skipped: number; not_followed: number;
    };
    next_due_at: string | null;
    last_error: string | null;
};

/** Crawls something is still working on: the screen polls faster while one is. */
export const CRAWLING: ReadonlySet<CrawlStatus> = new Set(['queued', 'running']);

/** Statuses something is still working on: the screen polls faster while any document is in one. */
export const WORKING: ReadonlySet<DocStatus> = new Set(['queued', 'parsing', 'indexing', 'deleting']);
/** Statuses a document rests in; a resting document can be retried. */
export const RESTING: ReadonlySet<DocStatus> = new Set(['indexed', 'partial', 'failed', 'skipped', 'paused', 'blocked']);

export const STATUS: Record<DocStatus, { label: string; tone: BadgeTone }> = {
    queued: { label: 'Queued', tone: 'neutral' },
    parsing: { label: 'Reading', tone: 'accent' },
    indexing: { label: 'Indexing', tone: 'accent' },
    indexed: { label: 'Indexed', tone: 'success' },
    partial: { label: 'Partly indexed', tone: 'warning' },
    failed: { label: 'Failed', tone: 'danger' },
    skipped: { label: 'Skipped', tone: 'neutral' },
    paused: { label: 'Paused', tone: 'warning' },
    blocked: { label: 'Blocked', tone: 'danger' },
    deleting: { label: 'Deleting', tone: 'neutral' },
};

/** A working document's stage, as a person reads it. */
export const PROGRESS_STAGE: Record<ProgressStage, string> = {
    parse: 'Reading',
    ocr: 'Reading scanned pages',
    enrich: 'Writing contexts',
    embed: 'Embedding',
    index: 'Indexing',
};

/** Time left, rounded as people read it; null when there is no estimate yet (no pace to estimate from). */
export function timeLeft(seconds: number | null | undefined): string | null {
    if (seconds == null || !Number.isFinite(seconds)) return null;
    if (seconds < 60) return 'less than a minute left';
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `about ${minutes} min left`;
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    return `about ${hours} h${rest ? ` ${rest} min` : ''} left`;
}

/** "12 of 357 passages", or null for a stage that cannot count. */
export function progressCount(p: { done: number | null; total: number | null; unit?: string | null }): string | null {
    if (p.done == null || p.total == null) return null;
    const unit = p.unit === 'pages' ? (p.total === 1 ? 'page' : 'pages') : (p.total === 1 ? 'passage' : 'passages');
    return `${p.done.toLocaleString()} of ${p.total.toLocaleString()} ${unit}`;
}

/** Whose rate limit the work is waiting out, until when, and that it goes on by itself. Our gateway's own name is
 *  the platform's. A document that gave its slot back to the wait (cloud, CCR-14) shows the same note while queued. */
export function waitNote(wait: ProgressWait | null | undefined): string | null {
    if (!wait) return null;
    const who = wait.provider_id === 'model_gateway' ? 'the platform' : wait.provider;
    const until = wait.until ? new Date(wait.until) : null;
    const at = until && !Number.isNaN(until.getTime())
        ? ` until ${until.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : '';
    return `Waiting for ${who}'s rate limit${at}; it carries on by itself.`;
}

/** Why a document is where it is, in a few words (the backend's `status_reason` codes). */
export const REASONS: Record<string, string> = {
    unsupported_format: 'Not a file type Synapse reads yet',
    over_size_cap: 'Larger than the per-file limit',
    over_page_cap: 'More pages than the per-file limit',
    empty_document: 'No text in it',
    excluded: 'Excluded',
    budget_cap: 'Monthly budget reached',
    wallet_402: 'Model wallet is empty',
    egress_blocked: 'Address refused',
    robots_disallowed: 'Disallowed by robots.txt',
    parser_timeout: 'Reading timed out',
    parser_memory: 'Too large to read',
    parser_crashed: 'Could not be read',
    encrypted: 'Encrypted',
    embed_failed: 'Embedding failed',
    embedding_invalid: 'The embedding model gave unusable vectors',
    enrich_failed: "The model writing passages' context failed",
    index_failed: 'Writing the index failed',
    fetch_failed: 'Could not be fetched',
    ocr_failed: 'Text recognition failed',
    internal: 'Something went wrong',
    truncated_pages: 'Some pages were cut',
    truncated_size: 'Cut at the size limit',
    no_text_layer: 'Some pages have no text layer',
    attachment_skipped: 'An attachment was skipped',
};

/** What an upload accepts: backend `core/knowledge/parse` ACCEPTED_EXTENSIONS, held equal by
 *  `tests/unit/test_knowledge_parse_formats.py`. */
export const ACCEPTED = [
    '.markdown', '.md', '.adoc', '.asciidoc', '.cfg', '.conf', '.ini', '.log', '.properties', '.rst', '.tex',
    '.text', '.toml', '.txt', '.bash', '.bat', '.c', '.cc', '.cjs', '.clj', '.cmake', '.cpp', '.cs', '.css', '.cxx',
    '.dart', '.erl', '.ex', '.exs', '.go', '.gradle', '.graphql', '.h', '.hcl', '.hpp', '.hs', '.java', '.jl',
    '.js', '.jsx', '.kt', '.kts', '.less', '.lua', '.m', '.mjs', '.php', '.pl', '.proto', '.ps1', '.py', '.pyi',
    '.r', '.rb', '.rs', '.scala', '.scss', '.sh', '.sql', '.svelte', '.swift', '.tf', '.ts', '.tsx', '.vue', '.zsh',
    '.json', '.csv', '.tsv', '.pdf', '.docm', '.docx', '.dotx', '.ppsx', '.pptm', '.pptx', '.xlsm', '.xlsx', '.odt',
    '.odp', '.ods', '.epub', '.rtf', '.htm', '.html', '.xhtml', '.xml', '.yaml', '.yml', '.eml', '.msg', '.jpeg',
    '.jpg', '.png', '.tif', '.tiff', '.webp', '.heic', '.heif', '.doc', '.ppt', '.xls',
];

/** The settings whose change rebuilds the index (backend `kb_settings.INDEX_AFFECTING`); everything else
 *  applies to the next search. Held equal by `tests/unit/test_knowledge_presets.py`. */
export const INDEX_AFFECTING = ['chunking', 'embedding', 'enrichment', 'language'] as const;

/** Keyword-search languages (backend `textsearch.TS_LANGUAGES`); "simple" stems nothing. Held equal by
 *  `tests/unit/test_knowledge_presets.py`. */
export const LANGUAGES = [
    'simple', 'arabic', 'danish', 'dutch', 'english', 'finnish', 'french', 'german', 'greek', 'hungarian',
    'indonesian', 'irish', 'italian', 'lithuanian', 'nepali', 'norwegian', 'portuguese', 'romanian', 'russian',
    'spanish', 'swedish', 'tamil', 'turkish',
] as const;

/** A named starting point for a base's settings: only the fields it names are set (B2b, D167). */
export type PipelinePreset = {
    id: string;
    name: string;
    description: string;
    fields: Record<string, Record<string, string | number | boolean | null>>;
};

/** Backend `core/knowledge/presets.py` PRESETS, as JSON. Held equal by `tests/unit/test_knowledge_presets.py`. */
export const PRESETS: PipelinePreset[] = [
    {
        "id": "cheap", "name": "Cheap",
        "description": "Larger chunks, no rerank, fewer passages. The least to embed and to search.",
        "fields": {
            "chunking": {"mode": "structure", "target_tokens": 600, "max_tokens": 1000, "overlap_pct": 0.0},
            "rerank": {"enabled": false, "top_n": 8, "candidates": 50},
            "fusion": {"overfetch": 20},
            "retrieval": {"top_k": 5, "parent_expansion": "off", "neighbor_window": 1, "max_context_tokens": 4000},
            "enrichment": {"mode": "prefix"}
        }
    },
    {
        "id": "balanced", "name": "Balanced",
        "description": "The defaults: section-sized chunks, rerank where one is configured, eight passages.",
        "fields": {
            "chunking": {"mode": "structure", "target_tokens": 450, "max_tokens": 800, "overlap_pct": 0.0},
            "rerank": {"enabled": null, "top_n": 8, "candidates": 50},
            "fusion": {"overfetch": 50},
            "retrieval": {"top_k": 8, "parent_expansion": "off", "neighbor_window": 1, "max_context_tokens": 6000},
            "enrichment": {"mode": "prefix"}
        }
    },
    {
        "id": "high_recall", "name": "High recall",
        "description": "Smaller overlapping chunks, more candidates and passages, each with its neighbours. More to embed and to read.",
        "fields": {
            "chunking": {"mode": "structure", "target_tokens": 300, "max_tokens": 600, "overlap_pct": 0.1},
            "rerank": {"enabled": null, "top_n": 12, "candidates": 100},
            "fusion": {"overfetch": 100},
            "retrieval": {"top_k": 12, "parent_expansion": "neighbors", "neighbor_window": 1, "max_context_tokens": 10000},
            "enrichment": {"mode": "prefix"}
        }
    }
];

/** `kb` with the preset's fields laid over its own (backend `presets.apply`). */
export function applyPreset<T extends Record<string, unknown>>(kb: T, preset: PipelinePreset): T {
    const out: Record<string, unknown> = { ...kb };
    for (const [block, fields] of Object.entries(preset.fields)) {
        out[block] = { ...(kb[block] as Record<string, unknown> | undefined), ...fields };
    }
    return out as T;
}

/** The preset whose every field `kb` has, or null for custom settings (backend `presets.matches`). */
export function matchingPreset(kb: Record<string, unknown>): PipelinePreset | null {
    return PRESETS.find(p => Object.entries(p.fields).every(([block, fields]) =>
        Object.entries(fields).every(([name, value]) =>
            ((kb[block] as Record<string, unknown> | undefined)?.[name] ?? null) === value))) ?? null;
}

/** An amount in USD as people read it: cents for a cent or more, two significant digits below that (a
 *  knowledge base's embedding often costs millionths), "$0" for nothing. */
export function formatUsd(n: number): string {
    if (!n) return '$0';
    if (n >= 0.01) return `$${n.toFixed(2)}`;
    if (n < 0.000001) return 'under $0.000001';
    const digits = Math.ceil(-Math.log10(n)) + 1;
    return `$${n.toFixed(digits).replace(/0+$/, '')}`;
}

/** The API's error, as one line. FastAPI answers `{detail: string}`, or a list for a validation error. */
export async function errorText(res: Response): Promise<string> {
    try {
        const body = await res.json();
        const detail = body?.detail;
        if (typeof detail === 'string') return detail;
        if (Array.isArray(detail) && detail.length) {
            return detail.map((d: { msg?: string }) => d?.msg).filter(Boolean).join('; ') || res.statusText;
        }
    } catch {
        /* not JSON */
    }
    return `${res.status} ${res.statusText}`.trim();
}

/** A refused call, with its status: a 409 means someone changed the base since it was read. */
export class KnowledgeApiError extends Error {
    constructor(message: string, readonly status: number) {
        super(message);
        this.name = 'KnowledgeApiError';
    }
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
    const res = await fetch(url, init);
    if (!res.ok) throw new KnowledgeApiError(await errorText(res), res.status);
    return res.json() as Promise<T>;
}

const json = (method: string, body: unknown): RequestInit => ({
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

/** A call answered with no body (204). */
async function callNone(url: string, init?: RequestInit): Promise<void> {
    const res = await fetch(url, init);
    if (!res.ok) throw new KnowledgeApiError(await errorText(res), res.status);
}

/** One passage a search found (`api_models.Hit`). */
export type SearchHit = {
    label: string;
    chunk_id: string;
    doc_id: string;
    kb_id: string;
    content: string;
    context: string;
    score: number;
    scores: Record<string, number>;
    metadata: Record<string, unknown>;
    citation: KnowledgeCitation;
};

/**
 * What decided a search's hits (`trace.Trace`), as far as the test search and a
 * Retrieve step's run log show it (`RetrievalTrace`). The fields past `timings_ms`
 * are optional only so an older trace still reads.
 */
export type SearchTrace = {
    query_id: string;
    degraded: { code: string; detail?: string | null }[];
    legs: { leg: string; requested: number; returned: number; filtered: boolean; exact: boolean; latency_ms: number }[];
    cuts: { stage: string; dropped_count: number; rule?: string | null }[];
    timings_ms: Record<string, number | null>;
    request?: { query: string; kb_ids: string[]; top_k: number; pinned_snapshots?: Record<string, string> };
    /** Per knowledge base: the index version that answered. */
    pipeline?: Record<string, { snapshot_id: string; embed_model?: string | null; rerank_model?: string | null }>;
    fusion?: { params: { k: number; weights: Record<string, number> } } | null;
    rerank?: { model: string; cost_source: string; input_count: number; latency_ms: number; results: { chunk_id: string }[] } | null;
    hits?: { label: string; path: string; page_start?: number | null; page_end?: number | null; score: number }[];
    costs?: {
        total_est_usd: number;
        total_charged_microusd?: number | null;
        rows: { stage: string; model: string; cost_source: string; est_usd: number }[];
    };
};

/** A stored search's trace (`GET /api/knowledge/queries/{id}`, kept 30 days) — a Retrieve step's, for its run log. */
export function getQueryTrace(queryId: string): Promise<SearchTrace> {
    return call<SearchTrace>(`/api/knowledge/queries/${encodeURIComponent(queryId)}`);
}

export type SearchResult = { query_id: string; hits: SearchHit[]; degraded: string[]; trace: SearchTrace | null };

/** Who serves a rerank model, by the prefix its id starts with (backend `core/knowledge/rerankers.py` ROUTES), and
 *  the setting that provider needs on Settings → Models (`null`: none). */
export type RerankRoute = { prefix: string; provider: string; key: string | null };

export const RERANK_ROUTES: RerankRoute[] = [
    { prefix: 'cohere.', provider: 'Cohere', key: 'cohere_key' },
    { prefix: 'voyage.', provider: 'Voyage AI', key: 'voyage_key' },
    { prefix: 'jina.', provider: 'Jina AI', key: 'jina_key' },
    { prefix: 'tei.', provider: 'Your rerank server (TEI or Infinity)', key: 'knowledge_rerank_base_url' },
];

/** One provider the workspace has a key for, with the rerank models that key can use, or why there are none
 *  (`GET /api/models/rerank`, BF9, D293): what the rerank pickers offer and the reranker cards show. */
export type RerankListing = {
    provider: string;
    label: string;
    /** The setting that lights it: its key, or the rerank server's address. */
    key: string;
    models: { id: string; label: string }[];
    /** Why nothing is listed: the key was refused, or the list could not be read. */
    error: string | null;
    /** The list stands, but something about the key is worth saying. */
    note: string | null;
};

/** The rerank pickers' options: every listed model, under its provider. */
export function rerankOptions(listings: readonly RerankListing[]): ComboboxOption[] {
    return listings.flatMap(l => l.models.map(m => ({ value: m.id, label: m.label, group: l.label })));
}

/** A reranker card's status: its model count, or why it has none. */
export function rerankStatus(listing: RerankListing | undefined, fallback: string): string {
    if (!listing) return fallback;
    if (listing.error) return listing.error;
    const n = listing.models.length;
    const count = `${n} rerank model${n === 1 ? '' : 's'}`;
    return listing.note ? `${count} · key not checked` : count;
}

/** Why a rebuild waits, in words. */
export const BUILD_PAUSED: Record<string, string> = {
    budget_cap: 'Paused: the monthly budget is reached. Raise it to go on, or it resumes next month.',
    wallet_402: 'Paused: the wallet is empty.',
    embedder_unavailable: 'Paused: the embedding model needs a key or address that is not saved.',
};

/** A search's degraded codes (`trace.DEGRADED_CODES`), in words. */
export const DEGRADED: Record<string, string> = {
    vector_unavailable: 'Matched by words only: search by meaning needs an embedding model.',
    lexical_unavailable: 'Word matching is not available in this index.',
    post_filtered: 'The filter was applied after the search, so fewer results may show.',
    rerank_failed: 'Reranking failed; results are in search order.',
    rerank_unconfigured: 'No reranker is set; results are in search order.',
    ann_approximate: 'The index searched approximately.',
    snapshot_retired: 'This search used an index version that has been replaced.',
    truncated: 'The trace was shortened to fit.',
    budget_paused: "This knowledge base hit its monthly budget.",
    embed_failed: 'The question could not be embedded; words only.',
};

/** One stage of a cost preview (`api_models.StageEstimate`). */
export type StageEstimate = { stage: string; model: string; units: number; unit: string; est_usd: number; cost_source: string };

/** What re-indexing a base under a draft of its settings would cost (`api_models.CostPreview`). */
export type CostPreview = {
    docs: number;
    pages: number;
    stages: StageEstimate[];
    total_est_usd: number;
    budget_remaining_usd: number | null;
};

export type CostGroupBy = 'day' | 'stage' | 'document' | 'model';

/** A base's spend for one day, stage, document ("search" for searches) or model (`api_models.CostGroup`). */
export type CostGroup = { key: string; est_usd: number; charged_microusd: number | null; input_tokens: number; units: number };

/** The settings a base can be created with, besides its name and folder. */
export type BaseSettings = Pick<KnowledgeBase,
    'language' | 'visitor_access' | 'budget_monthly_usd' | 'chunking' | 'embedding' | 'rerank' | 'fusion' | 'retrieval'
    | 'enrichment'>;

// ── evaluation: test a knowledge base on its own documents (V1, D168) ──────────

export type EvalCaseKind = 'direct' | 'paraphrase' | 'multi_hop' | 'promoted';
export type EvalConfig = 'lexical' | 'vector' | 'hybrid' | 'served' | 'custom';
export type EvalRunStatus = 'queued' | 'running' | 'cancelling' | 'completed' | 'cancelled' | 'failed' | 'interrupted'
    | 'skipped';

/** Where a case's answer is written: a quote, and its span in the document (`api_models.EvalEvidence`). */
export type EvalEvidence = {
    doc_id: string; path: string; attachment?: string | null; doc_sha256?: string | null;
    char_start: number; char_end: number; quote: string;
};
export type EvalCase = {
    id?: string | null; kind: EvalCaseKind; question: string; reference_answer: string; evidence: EvalEvidence[];
    query_id?: string | null; overlap?: number | null; created_at?: string | null;
};
export type EvalScores = {
    cases: number; hit_at: Record<string, number>; mrr: number; ndcg_at_10: number; recall_at_10: number;
    complete_at_10: number;
};
/** One configuration's scores in one snapshot (`api_models.EvalConfigResult`). */
export type EvalConfigResult = {
    key: string; snapshot_id: string; config: EvalConfig; label: string; available: boolean; reason: string | null;
    weights: Record<string, number> | null; rrf_k: number | null; agent_sees: number | null;
    scores: EvalScores; by_kind: Record<string, EvalScores>;
};
export type EvalSnapshotInfo = {
    snapshot_id: string; status: string; docs_done: number | null; docs_total: number | null; comparable: boolean;
    embed_model: string | null; chunker_version: string | null; settings: Record<string, unknown>;
};
export type EvalJudgeSummary = {
    answer_model: string; judge_model: string; rubric_id: string; rubric_version: number | null; scale_max: number;
    mean_score: number | null; judged: number; failed: number; quotes: number; quotes_matched: number;
    unresolved_labels: number; warning: string | null;
};
export type EvalRunSummary = {
    id: string; kb_id: string; set_id: string; status: EvalRunStatus; started_by: 'person' | 'settings';
    case_count: number; done_count: number; compared_cases: number; stale_cases: number;
    snapshots: EvalSnapshotInfo[]; results: EvalConfigResult[]; judge: EvalJudgeSummary | null; notes: string[];
    est_usd: number | null; cost_usd: number | null; error: string | null;
    created_at: string | null; started_at: string | null; finished_at: string | null;
};
export type EvalVerdict = { score: number | null; reasoning: string; per_criterion: Record<string, { score: number; reasoning: string }>; error: string | null };
export type EvalCaseResult = {
    case_id: string; kind: EvalCaseKind; question: string; stale: boolean; trimmed: boolean;
    ranks: Record<string, number | null>; found: Record<string, number>; query_ids: Record<string, string>;
    top: { path: string; snippet: string; relevant: boolean }[];
    answer: string | null; quote_check: { quotes: number; matched: number; unresolved: number[] } | null;
    verdict: EvalVerdict | null;
};
export type EvalRun = EvalRunSummary & { cases: EvalCaseResult[] };
export type EvalSetSummary = {
    id: string; kb_id: string; name: string; revision: number; case_count: number;
    counts_by_kind: Record<string, number>; created_at: string | null; updated_at: string | null;
    last_run: EvalRunSummary | null;
};
export type EvalSet = EvalSetSummary & { cases: EvalCase[] };

/**
 * How often the index serving now put the answer in its top 5, from a question set's latest run: the base's
 * "Finds the answer" (M2e). Null when the set has never run, or its run could not score the serving index.
 */
export function servedHit5(set: Pick<EvalSetSummary, 'last_run'>): number | null {
    const run = set.last_run;
    const served = run?.results.find(r => r.config === 'served' && r.snapshot_id === run.snapshots[0]?.snapshot_id);
    const hit = served?.available ? served.scores.hit_at['5'] : undefined;
    return hit == null ? null : hit;
}
export type EvalDraft = { kind: EvalCaseKind; question: string; reference_answer: string; evidence: EvalEvidence[]; overlap: number | null };
export type EvalDrafts = { drafts: EvalDraft[]; model: string; dropped: Record<string, number>; saved: false };
export type EvalDraftRequest = { model?: string; direct: number; paraphrase: number; multi_hop: number; set_id?: string; seed?: number };
export type EvalJudgeRequest = { answer_model: string; judge_model: string };
export type EvalRunRequest = { set_id: string; snapshot_ids?: string[]; rerank?: boolean | null; judge?: EvalJudgeRequest | null };
export type EvalCostLine = {
    part: 'drafts' | 'search' | 'answers' | 'judge'; model: string; calls: number; input_tokens: number;
    output_tokens: number; est_usd: number | null; counts_toward_budget: boolean;
};
export type EvalEstimate = { lines: EvalCostLine[]; total_est_usd: number; unpriced: string[]; budget_remaining_usd: number | null };
export type EvalQuery = {
    query_id: string; query: string; surface: string; created_at: string; retained_by: string | null;
    hits: { label: string; chunk_id: string; doc_id: string; path: string; snippet: string; score: number | null }[];
};

export const EVAL_KINDS: Record<EvalCaseKind, string> = {
    direct: 'Direct', paraphrase: 'Paraphrase', multi_hop: 'Multi-hop', promoted: 'Real search',
};
/** Why the server dropped a draft, in words. */
export const DRAFT_DROPPED: Record<string, string> = {
    quote_not_found: 'the words it quoted are not in the passage',
    too_close_to_passage: 'it kept the passage\'s own words',
    unreadable: 'the model did not answer in the expected form',
    failed: 'the model call failed',
    timed_out: 'the model took too long',
    no_partner: 'no second document shares enough with the first',
    no_passage: 'there were no more passages to ask about',
};

const evalBase = (kbId: string) => `/api/knowledge/bases/${kbId}/eval`;

export const knowledgeApi = {
    bases: () => call<KnowledgeBase[]>('/api/knowledge/bases'),
    base: (id: string) => call<KnowledgeBase>(`/api/knowledge/bases/${id}`),
    createBase: (body: { name: string; folder: string; description?: string } & Partial<BaseSettings>) =>
        call<KnowledgeBase>('/api/knowledge/bases', json('POST', body)),
    deleteBase: (id: string) => call<{ status: string }>(`/api/knowledge/bases/${id}`, { method: 'DELETE' }),
    /** A patch from the revision the edit started at; a stale one is a 409. */
    updateBase: (id: string, patch: Partial<Omit<KnowledgeBase, 'id' | 'state'>> & { revision: number }) =>
        call<KnowledgeBase>(`/api/knowledge/bases/${id}`, json('PATCH', patch)),
    /** Fetch web pages into the base: each URL becomes a source (the same URL and kind again crawls it now). */
    addUrls: (kbId: string, body: {
        urls: string[]; kind: WebSourceKind; max_depth?: number; max_pages?: number; metadata?: Record<string, unknown>;
    }) => call<{ crawl_id: string; accepted: number; status: 'queued' }>(
        `/api/knowledge/bases/${kbId}/urls`, json('POST', body)),
    crawls: (kbId: string) => call<WebCrawlState[]>(`/api/knowledge/bases/${kbId}/crawls`),
    /** What re-indexing the base under its settings with `overrides` laid over them would cost. Starts nothing. */
    costPreview: (id: string, overrides: Partial<BaseSettings> | null, init?: { signal?: AbortSignal }) =>
        call<CostPreview>(`/api/knowledge/bases/${id}/cost-preview`, { ...json('POST', { overrides }), ...init }),
    /** What the base has cost this month (from `since`, an ISO date), grouped. */
    costs: (id: string, groupBy: CostGroupBy = 'day', since?: string) => {
        const q = new URLSearchParams({ group_by: groupBy, ...(since ? { since } : {}) });
        return call<CostGroup[]>(`/api/knowledge/bases/${id}/costs?${q}`);
    },
    documents: async (kbId: string) => {
        // Paged by id; a knowledge base's whole list, 1,000 at a time.
        const out: KnowledgeDocument[] = [];
        let after = '';
        for (;;) {
            const q = new URLSearchParams({ limit: '1000', ...(after ? { after } : {}) });
            const page = await call<KnowledgeDocument[]>(`/api/knowledge/bases/${kbId}/documents?${q}`);
            out.push(...page);
            if (page.length < 1000) return out;
            after = page[page.length - 1].id;
        }
    },
    document: (id: string) => call<KnowledgeDocumentDetail>(`/api/knowledge/documents/${id}`),
    retry: (id: string) => call<KnowledgeDocument>(`/api/knowledge/documents/${id}/retry`, { method: 'POST' }),
    deleteDocument: (id: string) => call<{ status: string }>(`/api/knowledge/documents/${id}`, { method: 'DELETE' }),
    /** What an agent searching these bases would find, with the trace that decided it. */
    search: (body: { query: string; kb_ids: string[]; top_k?: number; include_trace?: boolean }) =>
        call<SearchResult>('/api/knowledge/search', json('POST', body)),
    /** A base's eval (V1): question sets, drafts that are never saved, runs. */
    eval: {
        sets: (kbId: string) => call<EvalSetSummary[]>(`${evalBase(kbId)}/sets`),
        createSet: (kbId: string, body: { name: string; cases?: EvalCase[] }) =>
            call<EvalSet>(`${evalBase(kbId)}/sets`, json('POST', body)),
        set: (kbId: string, setId: string) => call<EvalSet>(`${evalBase(kbId)}/sets/${setId}`),
        /** A save from the revision the edit started at; a stale one is a 409. */
        saveSet: (kbId: string, setId: string, body: { revision: number; name?: string; cases?: EvalCase[] }) =>
            call<EvalSet>(`${evalBase(kbId)}/sets/${setId}`, json('PUT', body)),
        deleteSet: (kbId: string, setId: string) => callNone(`${evalBase(kbId)}/sets/${setId}`, { method: 'DELETE' }),
        promote: (kbId: string, setId: string, body: { query_id: string; chunk_ids: string[]; question?: string }) =>
            call<EvalSet>(`${evalBase(kbId)}/sets/${setId}/promote`, json('POST', body)),
        /** Paid: one model call per question. Saves nothing. */
        drafts: (kbId: string, body: EvalDraftRequest) => call<EvalDrafts>(`${evalBase(kbId)}/drafts`, json('POST', body)),
        estimate: (kbId: string, body: { drafts: EvalDraftRequest } | { run: EvalRunRequest }, init?: { signal?: AbortSignal }) =>
            call<EvalEstimate>(`${evalBase(kbId)}/estimate`, { ...json('POST', body), ...init }),
        queries: (kbId: string) => call<EvalQuery[]>(`${evalBase(kbId)}/queries`),
        startRun: (kbId: string, body: EvalRunRequest) => call<EvalRunSummary>(`${evalBase(kbId)}/runs`, json('POST', body)),
        runs: (kbId: string, setId: string) =>
            call<EvalRunSummary[]>(`${evalBase(kbId)}/runs?${new URLSearchParams({ set_id: setId })}`),
        run: (kbId: string, runId: string) => call<EvalRun>(`${evalBase(kbId)}/runs/${runId}`),
        cancelRun: (kbId: string, runId: string) =>
            call<EvalRunSummary>(`${evalBase(kbId)}/runs/${runId}/cancel`, { method: 'POST' }),
        /** The run's searches fused again with other weights: no search, $0. */
        compare: (kbId: string, runId: string, body: { weights: Record<string, number>; rrf_k: number }) =>
            call<EvalConfigResult[]>(`${evalBase(kbId)}/runs/${runId}/compare`, json('POST', body)),
    },
};

/** Where one part's bytes go (`api_models.UploadPart`). OSS serves the URL itself; cloud's is a presigned
 *  object-store URL. `headers` are sent with the PUT exactly as given, and nothing else is. */
export type UploadPart = { part_number: number; url: string; method?: 'PUT'; headers?: Record<string, string> };

/** An upload in parts (`api_models.UploadSession`): every part is `part_size` bytes but the last. */
export type UploadSession = { upload_id: string; part_size: number; parts: UploadPart[]; expires_at?: string };

/** Fresh URLs for parts whose signature ran out (a part's PUT answered 403). Only a product whose part URLs
 *  expire supplies one; without it a 403 fails the upload. */
export type RefreshParts = (uploadId: string, partNumbers: number[]) => Promise<UploadPart[]>;

/** A part's answer: its status, the `ETag` header and the body. */
type PartAnswer = { status: number; etag: string | null; text: string };

/**
 * PUT one part, with exactly its own headers. XHR, not `fetch`: only XHR hears an upload's bytes as they go
 * (`onSent`), and a part is up to 8 MiB. No credentials cross-origin (an object store's presigned URL takes
 * none), as `fetch` sends none.
 */
function putPart(part: UploadPart, body: Blob, onSent?: (bytes: number) => void): Promise<PartAnswer> {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open(part.method ?? 'PUT', part.url);
        for (const [name, value] of Object.entries(part.headers ?? {})) xhr.setRequestHeader(name, value);
        if (onSent) xhr.upload.onprogress = e => onSent(e.loaded);
        xhr.onload = () => resolve({ status: xhr.status, etag: xhr.getResponseHeader('ETag'), text: xhr.responseText });
        xhr.onerror = () => reject(new Error('The upload was cut off; check the connection and try again.'));
        xhr.onabort = () => reject(new Error('The upload was stopped.'));
        xhr.send(body);
    });
}

/**
 * The part's ETag, as complete wants it back: the `ETag` header, quotes and all —
 * an object store answers a part with that header and an empty body — else the
 * `etag` of OSS's JSON answer.
 */
function partEtag(res: PartAnswer): string {
    if (res.etag) return res.etag;
    try {
        const body = JSON.parse(res.text) as { etag?: unknown } | null;
        if (typeof body?.etag === 'string' && body.etag) return body.etag;
    } catch {
        /* not JSON */
    }
    throw new Error('The upload answered without the part\'s ETag.');
}

/** A refused part's reason, as `errorText` reads an API's answer. */
function partError(res: PartAnswer): string {
    try {
        const detail = (JSON.parse(res.text) as { detail?: unknown } | null)?.detail;
        if (typeof detail === 'string' && detail) return detail;
    } catch {
        /* an object store answers in XML */
    }
    return `The upload of a part was refused (${res.status}).`;
}

/** Give up an upload: its parts go, and it no longer counts as open (F341, CCR-12). Best effort — an upload
 *  not cancelled expires by itself — and it never hides the failure that led to it. */
async function cancelUpload(uploadId: string): Promise<void> {
    try {
        await fetch(`/api/knowledge/uploads/${uploadId}`, { method: 'DELETE' });
    } catch {
        /* expires by itself */
    }
}

/**
 * Upload one file into a knowledge base, in parts. `folder` is inside the base
 * (its root by default). `onProgress` hears the bytes sent so far as they go. A
 * part refused with 403 gets one fresh URL through `refreshParts`, when given.
 *
 * An upload that cannot finish is cancelled (F341): when a part fails, and when
 * the complete is refused — never after a complete that may have gone through
 * (no answer), nor one refused for now (408, 429), which can be sent again.
 */
export async function uploadToKnowledge(
    kbId: string,
    file: File,
    { folder, onProgress, refreshParts }: {
        folder?: string; onProgress?: (sent: number, total: number) => void; refreshParts?: RefreshParts;
    } = {},
): Promise<KnowledgeDocument[]> {
    if (file.size === 0) throw new Error('The file is empty.');
    const session = await call<UploadSession>(`/api/knowledge/bases/${kbId}/uploads`, json('POST', {
        filename: file.name, size_bytes: file.size, mime: file.type || 'application/octet-stream',
        ...(folder ? { path: folder } : {}),
    }));
    const done: { part_number: number; etag: string }[] = [];
    try {
        let sent = 0;
        for (const part of session.parts) {
            const start = (part.part_number - 1) * session.part_size;
            const blob = file.slice(start, start + session.part_size);
            const heard = (bytes: number) => onProgress?.(sent + Math.min(bytes, blob.size), file.size);
            let res = await putPart(part, blob, heard);
            if (res.status === 403 && refreshParts) {
                const fresh = (await refreshParts(session.upload_id, [part.part_number]))
                    .find(p => p.part_number === part.part_number);
                if (fresh) res = await putPart(fresh, blob, heard);
            }
            if (res.status < 200 || res.status >= 300) throw new Error(partError(res));
            done.push({ part_number: part.part_number, etag: partEtag(res) });
            sent += blob.size;
            onProgress?.(sent, file.size);
        }
    } catch (e) {
        await cancelUpload(session.upload_id);
        throw e;
    }
    const res = await fetch(`/api/knowledge/uploads/${session.upload_id}/complete`, json('POST', { parts: done }));
    if (!res.ok) {
        const refused = new KnowledgeApiError(await errorText(res), res.status);
        if (res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429) {
            await cancelUpload(session.upload_id);
        }
        throw refused;
    }
    const accepted = await res.json() as { documents: KnowledgeDocument[] };
    return accepted.documents;
}

/** Dispatched on `window` once a saved setting switched Knowledge on or off (OSS's Settings → General, F367),
 *  so every screen that reads the switch reads it again. */
export const KNOWLEDGE_SWITCHED_EVENT = 'synapse-knowledge-switched';

/** Whether Knowledge is switched on (`knowledge_enabled`); null until known. */
export function useKnowledgeEnabled(): boolean | null {
    const [enabled, setEnabled] = useState<boolean | null>(null);
    useEffect(() => {
        let live = true;
        const read = () => fetch('/api/settings')
            .then(r => r.json())
            .then(data => { if (live) setEnabled(!!data?.knowledge_enabled); })
            .catch(() => { if (live) setEnabled(false); });
        read();
        window.addEventListener(KNOWLEDGE_SWITCHED_EVENT, read);
        return () => { live = false; window.removeEventListener(KNOWLEDGE_SWITCHED_EVENT, read); };
    }, []);
    return enabled;
}

/**
 * Every folder `GET /api/vault/tree` holds, from either shape it comes in: a
 * nested `{tree: [{type, path, children}]}` (OSS), in tree order, or a flat
 * `{folders: [...], files: [{path}]}` (cloud), where a folder that only holds
 * files is named by its files, sorted.
 */
export function vaultFolders(data: unknown): string[] {
    const d = (data ?? {}) as {
        tree?: { type?: string; path?: string; children?: unknown[] }[];
        folders?: unknown[];
        files?: { path?: unknown }[];
    };
    if (Array.isArray(d.tree)) {
        const out: string[] = [];
        const walk = (nodes: { type?: string; path?: string; children?: unknown[] }[]) => {
            for (const n of nodes) {
                if (n?.type !== 'folder' || typeof n.path !== 'string') continue;
                out.push(n.path);
                walk((n.children ?? []) as typeof nodes);
            }
        };
        walk(d.tree);
        return out;
    }
    const seen = new Set<string>();
    const withParents = (path: string) => {
        const parts = path.split('/').filter(Boolean);
        for (let i = 1; i <= parts.length; i++) seen.add(parts.slice(0, i).join('/'));
    };
    for (const f of Array.isArray(d.folders) ? d.folders : []) if (typeof f === 'string') withParents(f);
    for (const f of Array.isArray(d.files) ? d.files : []) {
        if (typeof f?.path !== 'string') continue;
        const cut = f.path.lastIndexOf('/');
        if (cut > 0) withParents(f.path.slice(0, cut));
    }
    return [...seen].sort();
}

/** A path as shown inside its knowledge base: without the base's folder in front. */
export function inBase(path: string, folder: string): string {
    return path.startsWith(`${folder}/`) ? path.slice(folder.length + 1) : path;
}

export function formatBytes(n: number | null | undefined): string {
    if (n == null) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
