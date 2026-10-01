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

import type { BadgeTone } from '@/components/ui';
import type { KnowledgeCitation } from '@/types';

export type DocStatus =
    | 'queued' | 'parsing' | 'indexing' | 'indexed' | 'partial' | 'failed' | 'skipped' | 'paused' | 'blocked'
    | 'deleting';

export type KnowledgeBaseState = {
    counts_by_status: Partial<Record<DocStatus, number>>;
    active_snapshot: string | null;
    building_snapshot: string | null;
    month_spend_usd: number;
    paused_reason: string | null;
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
    revision: number;
    created_at: string;
    updated_at: string;
    state: KnowledgeBaseState;
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

async function call<T>(url: string, init?: RequestInit): Promise<T> {
    const res = await fetch(url, init);
    if (!res.ok) throw new Error(await errorText(res));
    return res.json() as Promise<T>;
}

const json = (method: string, body: unknown): RequestInit => ({
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

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

export const knowledgeApi = {
    bases: () => call<KnowledgeBase[]>('/api/knowledge/bases'),
    createBase: (body: { name: string; folder: string; description?: string }) =>
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
};

/**
 * Upload one file into a knowledge base, in parts. `folder` is inside the base
 * (its root by default). `onProgress` hears bytes sent after each part.
 */
export async function uploadToKnowledge(
    kbId: string,
    file: File,
    { folder, onProgress }: { folder?: string; onProgress?: (sent: number, total: number) => void } = {},
): Promise<KnowledgeDocument[]> {
    if (file.size === 0) throw new Error('The file is empty.');
    const session = await call<{
        upload_id: string; part_size: number; parts: { part_number: number; url: string }[];
    }>(`/api/knowledge/bases/${kbId}/uploads`, json('POST', {
        filename: file.name, size_bytes: file.size, mime: file.type || 'application/octet-stream',
        ...(folder ? { path: folder } : {}),
    }));
    const done: { part_number: number; etag: string }[] = [];
    let sent = 0;
    for (const part of session.parts) {
        const start = (part.part_number - 1) * session.part_size;
        const blob = file.slice(start, start + session.part_size);
        const res = await fetch(part.url, { method: 'PUT', body: blob });
        if (!res.ok) throw new Error(await errorText(res));
        const { etag } = await res.json() as { etag: string };
        done.push({ part_number: part.part_number, etag });
        sent += blob.size;
        onProgress?.(sent, file.size);
    }
    const accepted = await call<{ documents: KnowledgeDocument[] }>(
        `/api/knowledge/uploads/${session.upload_id}/complete`, json('POST', { parts: done }));
    return accepted.documents;
}

/** Whether Knowledge is switched on (`knowledge_enabled`); null until known. */
export function useKnowledgeEnabled(): boolean | null {
    const [enabled, setEnabled] = useState<boolean | null>(null);
    useEffect(() => {
        let live = true;
        fetch('/api/settings')
            .then(r => r.json())
            .then(data => { if (live) setEnabled(!!data?.knowledge_enabled); })
            .catch(() => { if (live) setEnabled(false); });
        return () => { live = false; };
    }, []);
    return enabled;
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
