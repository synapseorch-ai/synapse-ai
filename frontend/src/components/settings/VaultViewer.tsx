'use client';
/**
 * How the Vault shows a file, by its kind (F1207, Naveen 10-29: "a better editor and viewer to see more files and
 * edit files"). Text of every kind opens in an editor; a table as a table; a raster image as itself; anything else
 * says what it is and downloads. Images come from `/api/vault/raw`, which serves only raster types inline and
 * everything else as an attachment, so an SVG or an HTML file never runs here.
 */
import React, { useMemo, useState } from 'react';
import { Download } from 'lucide-react';
import { buttonStyles, DataTable, EmptyState, Segmented, type Column, type CodeLanguage } from '@/components/ui';
import { cn } from '@/lib/cn';

export type FileKind = 'markdown' | 'json' | 'table' | 'code' | 'image' | 'pdf';

/** Shown in the page by `/api/vault/raw` (its `_INLINE_TYPES`). */
const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif']);
const TABLE_EXTS = new Set(['.csv', '.tsv']);
const MARKDOWN_EXTS = new Set(['.md', '.markdown', '.mdx']);

/** A file's kind by its extension. A text file of any other kind is `code`: an editor with line numbers. */
export function fileKind(ext: string | undefined): FileKind {
    const e = (ext ?? '').toLowerCase();
    if (MARKDOWN_EXTS.has(e)) return 'markdown';
    if (e === '.json') return 'json';
    if (TABLE_EXTS.has(e)) return 'table';
    if (IMAGE_EXTS.has(e)) return 'image';
    if (e === '.pdf') return 'pdf';
    return 'code';
}

/** The editor's colouring for a code kind: the languages the product's editor has. */
export function codeLanguage(ext: string | undefined): CodeLanguage {
    return (ext ?? '').toLowerCase() === '.py' ? 'python' : 'text';
}

export function rawUrl(path: string, source: string, download = false): string {
    return `/api/vault/raw?path=${encodeURIComponent(path)}&source=${source}${download ? '&download=true' : ''}`;
}

export function formatSize(bytes: number | null | undefined): string | null {
    if (bytes == null) return null;
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
    return `${(bytes / 1048576).toFixed(1)} MB`;
}

export function formatWhen(iso: string | null | undefined): string | null {
    if (!iso) return null;
    const when = new Date(iso);
    if (Number.isNaN(when.getTime())) return null;
    return when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/** A plain link with a button's look: `download` on an <a> is what saves the file under its own name. */
export function DownloadButton({ href, name, variant = 'secondary' }: { href: string; name: string; variant?: 'secondary' | 'primary' }) {
    return (
        <a href={href} download={name} className={cn(buttonStyles({ variant }), 'gap-1.5')}>
            <Download className="size-4" aria-hidden /> Download
        </a>
    );
}

/** A file the page does not show: what it is, and its download. */
export function DownloadOnly({ title, children, href, name }: { title: string; children: React.ReactNode; href: string; name: string }) {
    return (
        <EmptyState title={title} action={<DownloadButton href={href} name={name} variant="primary" />}>
            {children}
        </EmptyState>
    );
}

export function ImageView({ src, name }: { src: string; name: string }) {
    const [broken, setBroken] = useState(false);
    if (broken) return <EmptyState title="Can't show this image">It may be damaged; download it to look.</EmptyState>;
    return (
        <div className="flex min-h-60 items-center justify-center rounded-md border border-border bg-surface-2 p-4">
            {/* eslint-disable-next-line @next/next/no-img-element -- a Vault file, served by the API, not a site asset */}
            <img src={src} alt={name} onError={() => setBroken(true)} className="max-h-[calc(100vh-330px)] max-w-full object-contain" />
        </div>
    );
}

/** RFC 4180 rows: quoted fields may hold the separator, a newline or a doubled quote. */
export function parseTable(text: string, separator: string): string[][] {
    const rows: string[][] = [];
    let row: string[] = [];
    let field = '';
    let quoted = false;
    for (let i = 0; i < text.length; i++) {
        const ch = text[i];
        if (quoted) {
            if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
            else if (ch === '"') quoted = false;
            else field += ch;
        } else if (ch === '"' && field === '') quoted = true;
        else if (ch === separator) { row.push(field); field = ''; }
        else if (ch === '\n' || ch === '\r') {
            if (ch === '\r' && text[i + 1] === '\n') i++;
            row.push(field); rows.push(row); row = []; field = '';
        } else field += ch;
    }
    if (field !== '' || row.length > 0) { row.push(field); rows.push(row); }
    return rows;
}

/** The most rows a table shows at once: the Text tab has them all. */
const TABLE_ROWS = 1000;

type TableRow = { id: string; cells: string[] };

function sortValue(cell: string | undefined): string | number {
    const n = Number((cell ?? '').replace(/,/g, ''));
    return cell && cell.trim() !== '' && Number.isFinite(n) ? n : (cell ?? '').toLowerCase();
}

/** A CSV or TSV as a table (its first row the header), with its text one tab away. */
export function TableView({ ext, editor, text }: { ext: string; editor: React.ReactNode; text: string }) {
    const [view, setView] = useState<'table' | 'text'>('table');
    const { columns, rows, total } = useMemo(() => {
        const parsed = parseTable(text, ext === '.tsv' ? '\t' : ',');
        const [head = [], ...body] = parsed;
        const width = Math.max(head.length, ...body.slice(0, TABLE_ROWS).map(r => r.length), 0);
        const cols: Column<TableRow>[] = Array.from({ length: width }, (_, i) => ({
            id: String(i),
            header: head[i] || `Column ${i + 1}`,
            cell: (r: TableRow) => r.cells[i] ?? '',
            sortBy: (r: TableRow) => sortValue(r.cells[i]),
        }));
        return {
            columns: cols,
            rows: body.slice(0, TABLE_ROWS).map((cells, i) => ({ id: String(i), cells })),
            total: body.length,
        };
    }, [text, ext]);

    return (
        <div className="grid min-w-0 gap-3">
            <Segmented
                label="Show as"
                value={view}
                onChange={setView}
                options={[{ value: 'table', label: 'Table' }, { value: 'text', label: 'Text' }]}
            />
            {view === 'text' ? editor : columns.length === 0 ? (
                <EmptyState title="Empty table">Switch to Text to write its first row.</EmptyState>
            ) : (
                <div className="grid min-w-0 gap-2">
                    <div className="max-h-[calc(100vh-380px)] min-h-60 overflow-auto modern-scrollbar">
                        <DataTable
                            rows={rows}
                            columns={columns}
                            getRowId={r => r.id}
                            searchKeys={r => r.cells}
                            searchPlaceholder="Filter rows…"
                            density="compact"
                            minWidth={`${Math.min(Math.max(columns.length * 9, 24), 160)}rem`}
                        />
                    </div>
                    <p className="m-0 text-xs text-text-faint">
                        {total > TABLE_ROWS
                            ? `The first ${TABLE_ROWS.toLocaleString()} of ${total.toLocaleString()} rows. Text has them all.`
                            : `${total.toLocaleString()} row${total === 1 ? '' : 's'}, ${columns.length} column${columns.length === 1 ? '' : 's'}.`}
                    </p>
                </div>
            )}
        </div>
    );
}
