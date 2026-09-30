/*
 * Knowledge (D157): knowledge bases, their documents, and uploads — the client side of
 * `backend/core/routes/knowledge.py`, whose bodies are frozen in
 * `backend/core/knowledge/api_models.py`.
 *
 * A knowledge base is a Vault folder made searchable. Each file in it is a
 * document with a status; a document's report says what happened to it, stage
 * by stage. Uploads go in parts (at most 8 MiB each, under the proxy's body
 * limit): open a session, PUT each part, complete.
 */
import { useEffect, useState } from 'react';

import type { BadgeTone } from '@/components/ui';

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

export type KnowledgeBase = {
    id: string;
    name: string;
    description: string;
    folder: string;
    status: 'active' | 'disabled';
    language: string;
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

/** What an upload accepts (backend `core/knowledge/parse` ACCEPTED_EXTENSIONS). */
export const ACCEPTED = [
    '.md', '.markdown', '.txt', '.json', '.csv', '.tsv', '.pdf', '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
    '.odt', '.ods', '.odp', '.rtf', '.html', '.htm', '.eml', '.msg', '.epub', '.xml', '.yaml', '.yml', '.png',
    '.jpg', '.jpeg', '.tif', '.tiff', '.heic', '.webp',
];

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

export const knowledgeApi = {
    bases: () => call<KnowledgeBase[]>('/api/knowledge/bases'),
    createBase: (body: { name: string; folder: string; description?: string }) =>
        call<KnowledgeBase>('/api/knowledge/bases', json('POST', body)),
    deleteBase: (id: string) => call<{ status: string }>(`/api/knowledge/bases/${id}`, { method: 'DELETE' }),
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
