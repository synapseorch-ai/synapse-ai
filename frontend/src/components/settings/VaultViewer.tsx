'use client';
/**
 * How the Vault shows a file, by its kind (F1207, Naveen 10-29: "a better editor and viewer to see more files and
 * edit files"). Text of every kind opens in an editor; a table as a table; a raster image as itself; anything else
 * says what it is and downloads. Images come from `/api/vault/raw`, which serves only raster types inline and
 * everything else as an attachment, so an SVG or an HTML file never runs here.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Download } from 'lucide-react';
import {
    BarList, buttonStyles, DataTable, EmptyState, Field, Segmented, Select, StatStrip, type Column, type CodeLanguage,
} from '@/components/ui';
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

/** A CSV or TSV as a table (its first row the header), as a chart of its columns, or as its text. */
export function TableView({ ext, editor, text }: { ext: string; editor: React.ReactNode; text: string }) {
    const [view, setView] = useState<'table' | 'chart' | 'text'>('table');
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
                options={[{ value: 'table', label: 'Table' }, { value: 'chart', label: 'Chart' }, { value: 'text', label: 'Text' }]}
            />
            {view === 'text' ? editor : columns.length === 0 ? (
                <EmptyState title="Empty table">Switch to Text to write its first row.</EmptyState>
            ) : view === 'chart' ? (
                <ChartView text={text} separator={ext === '.tsv' ? '\t' : ','} />
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

// ---------------------------------------------------------------------------
// The Chart view (F1204, Naveen 10-29): a table's columns drawn, in the Vault
// ---------------------------------------------------------------------------
//
// Boards stay agents' memory and hand-offs; a chart is for a person looking at data, so it is drawn where the data
// is. An agent writes its numbers as a CSV here (the Vault's own tools) and a board note names it, @[path]. Nothing is
// saved by drawing it: the picks are the viewer's own.

export type ChartType = 'bar' | 'line' | 'number';
export type ChartAgg = 'sum' | 'count' | 'avg' | 'none';
export type ChartPoint = { label: string; value: number };

/** The most points drawn: a chart is a picture, not the table. */
export const CHART_POINTS = 500;

function numberOf(cell: string | undefined): number | null {
    const raw = (cell ?? '').replace(/,/g, '').trim();
    if (raw === '') return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
}

/**
 * A table's points: each value of column `x` once, in first-seen order, with column `y` summed, counted or averaged
 * over its rows; or, with `none`, each row as it is. A row whose value is not a number is left out and counted.
 */
export function tablePoints(rows: string[][], x: number, y: number | null, agg: ChartAgg):
    { points: ChartPoint[]; skipped: number } {
    const groups = new Map<string, number[]>();
    const points: ChartPoint[] = [];
    let skipped = 0;
    for (const row of rows) {
        if (!row.some(c => c.trim())) continue;
        const label = (row[x] ?? '').trim() || '(blank)';
        const value = agg === 'count' && y === null ? 1 : numberOf(y === null ? undefined : row[y]);
        if (value === null) { skipped += 1; continue; }
        if (agg === 'none') points.push({ label, value });
        else groups.set(label, [...(groups.get(label) ?? []), value]);
    }
    if (agg !== 'none') {
        for (const [label, values] of groups) {
            const total = agg === 'count' ? values.length : values.reduce((a, b) => a + b, 0);
            points.push({ label, value: agg === 'avg' ? total / values.length : total });
        }
    }
    return { points, skipped };
}

/** One number from a column: its sum, count or average over every row. */
export function tableNumber(rows: string[][], y: number | null, agg: ChartAgg): number | null {
    const values = rows.filter(r => r.some(c => c.trim()))
        .map(r => (y === null ? 1 : numberOf(r[y]))).filter((v): v is number => v !== null);
    if (!values.length) return null;
    if (agg === 'count') return values.length;
    const sum = values.reduce((a, b) => a + b, 0);
    return agg === 'avg' ? sum / values.length : sum;
}

const shown = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 2 });

/** The first column whose cells are mostly numbers, else none: what a chart's values default to. */
function firstNumeric(head: string[], rows: string[][], not: number): number | null {
    const sample = rows.slice(0, 50);
    for (let i = 0; i < head.length; i++) {
        if (i === not) continue;
        const numbers = sample.filter(r => numberOf(r[i]) !== null).length;
        if (sample.length && numbers / sample.length >= 0.8) return i;
    }
    return null;
}

export function ChartView({ text, separator }: { text: string; separator: string }) {
    const { head, rows } = useMemo(() => {
        const [first = [], ...rest] = parseTable(text, separator);
        return { head: first.map((h, i) => h.trim() || `Column ${i + 1}`), rows: rest };
    }, [text, separator]);
    const [type, setType] = useState<ChartType>('bar');
    const [x, setX] = useState(0);
    const [y, setY] = useState<number | null>(() => firstNumeric(head, rows, 0));
    const [agg, setAgg] = useState<ChartAgg>('sum');
    const columns = head.map((h, i) => ({ value: String(i), label: h }));
    const needsY = !(agg === 'count');
    const { points, skipped } = useMemo(() => tablePoints(rows, x, needsY ? y : null, agg), [rows, x, y, agg, needsY]);
    const number = useMemo(() => tableNumber(rows, needsY ? y : null, agg), [rows, y, agg, needsY]);
    const valueName = agg === 'count' ? 'rows' : y !== null ? head[y] : '';
    const aggWord = { sum: 'Total', count: 'Count of', avg: 'Average', none: '' }[agg];

    return (
        <div className="grid min-w-0 gap-4">
            <div className="flex flex-wrap items-end gap-3">
                <Segmented
                    label="Chart"
                    value={type}
                    onChange={setType}
                    options={[{ value: 'bar', label: 'Bar' }, { value: 'line', label: 'Line' }, { value: 'number', label: 'Number' }]}
                />
                {type !== 'number' && (
                    <Field label="Labels" className="w-44">
                        <Select size="sm" aria-label="Labels" value={String(x)} options={columns} onChange={v => setX(Number(v))} />
                    </Field>
                )}
                <Field label="Values" className="w-44">
                    <Select size="sm" aria-label="Values" value={y === null ? undefined : String(y)} placeholder="Pick a column"
                            options={columns} disabled={!needsY} onChange={v => setY(Number(v))} />
                </Field>
                <Field label="Each label's value" className="w-44">
                    <Select<ChartAgg> size="sm" aria-label="Each label's value" value={agg} onChange={setAgg}
                            options={[{ value: 'sum', label: 'Sum' }, { value: 'count', label: 'Count of rows' },
                                      { value: 'avg', label: 'Average' },
                                      ...(type === 'number' ? [] : [{ value: 'none' as ChartAgg, label: 'Each row as it is' }])]} />
                </Field>
            </div>

            {needsY && y === null ? (
                <EmptyState title="Pick the values">No column here is mostly numbers: pick the one to draw.</EmptyState>
            ) : type === 'number' ? (
                number === null ? <EmptyState title="No numbers">That column has no numbers to add up.</EmptyState> : (
                    <StatStrip label="The file's number" cells={[{ label: `${aggWord} ${valueName}`.trim(), value: shown(number),
                                                                   sub: `over ${rows.length.toLocaleString()} rows` }]} />
                )
            ) : points.length === 0 ? (
                <EmptyState title="Nothing to draw">No row has a number in {valueName}.</EmptyState>
            ) : (
                <figure className="m-0 grid min-w-0 gap-2">
                    {type === 'bar' ? (
                        <div className="max-h-[calc(100vh-420px)] min-h-40 overflow-auto modern-scrollbar">
                            <BarList label={`${valueName} by ${head[x]}`}
                                     rows={points.slice(0, CHART_POINTS).map((p, i) => ({ key: `${i}`, label: p.label, value: Math.max(0, p.value), display: shown(p.value) }))} />
                        </div>
                    ) : (
                        <LineChart points={points.slice(0, CHART_POINTS)} label={`${valueName} by ${head[x]}`} />
                    )}
                    <figcaption className="text-xs text-text-faint">
                        {`${aggWord ? `${aggWord} ` : ''}${valueName} by ${head[x]}`}
                        {` · ${points.length.toLocaleString()} ${points.length === 1 ? 'label' : 'labels'}`}
                        {points.length > CHART_POINTS && ` (the first ${CHART_POINTS} drawn)`}
                        {skipped > 0 && ` · ${skipped.toLocaleString()} row${skipped === 1 ? '' : 's'} without a number left out`}
                    </figcaption>
                </figure>
            )}
        </div>
    );
}

/** Values in order, left to right: drawn at the width it is shown at (as ScoreTrend draws a run's scores). */
function LineChart({ points, label }: { points: ChartPoint[]; label: string }) {
    const box = useRef<HTMLDivElement>(null);
    const [width, setWidth] = useState(640);
    useEffect(() => {
        const el = box.current;
        if (!el || typeof ResizeObserver === 'undefined') return;
        const watch = new ResizeObserver(([entry]) => {
            const w = Math.round(entry.contentRect.width);
            if (w > 0) setWidth(w);
        });
        watch.observe(el);
        return () => watch.disconnect();
    }, []);
    const height = 220;
    const padX = 8;
    const padY = 16;
    const values = points.map(p => p.value);
    const lo = Math.min(0, ...values);
    const hi = Math.max(...values, lo + 1);
    const step = points.length > 1 ? (width - padX * 2) / (points.length - 1) : 0;
    const px = (i: number) => padX + i * step;
    const py = (v: number) => height - padY - ((v - lo) / (hi - lo)) * (height - padY * 2);
    const first = points[0];
    const last = points[points.length - 1];
    return (
        <div ref={box} className="grid gap-1">
            <div className="flex justify-between text-xs tabular-nums text-text-faint"><span>{shown(hi)}</span></div>
            <svg viewBox={`0 0 ${width} ${height}`} className="h-56 w-full overflow-visible" role="img"
                 aria-label={`${label}: ${points.length} points, from ${shown(first.value)} to ${shown(last.value)}`}>
                <line x1={padX} x2={width - padX} y1={py(lo)} y2={py(lo)} className="stroke-border" strokeWidth={1} vectorEffect="non-scaling-stroke" />
                <polyline points={points.map((p, i) => `${px(i).toFixed(1)},${py(p.value).toFixed(1)}`).join(' ')}
                          fill="none" stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round"
                          vectorEffect="non-scaling-stroke" className="text-machine" />
                {points.map((p, i) => (
                    <circle key={i} cx={px(i)} cy={py(p.value)} r={Math.max(1, Math.min(3, step / 4))} className="fill-machine">
                        <title>{`${p.label}: ${shown(p.value)}`}</title>
                    </circle>
                ))}
            </svg>
            <div className="flex justify-between gap-4 text-xs text-text-faint">
                <span className="truncate">{first.label}</span>
                <span className="tabular-nums">{shown(lo)}</span>
                <span className="truncate text-right">{last.label}</span>
            </div>
        </div>
    );
}
