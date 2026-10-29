'use client';
/**
 * How the Vault shows a file, by its kind (F1207, Naveen 10-29: "a better editor and viewer to see more files and
 * edit files"). Text of every kind opens in an editor; a table as a table; a raster image as itself; anything else
 * says what it is and downloads. Images come from `/api/vault/raw`, which serves only raster types inline and
 * everything else as an attachment, so an SVG or an HTML file never runs here.
 */
import React, { useState } from 'react';
import { Download } from 'lucide-react';
import { buttonStyles, EmptyState } from '@/components/ui';
import { cn } from '@/lib/cn';

export { codeLanguage, fileKind, type FileKind } from '@/components/vault/kinds';
export { ChartView, parseTable, TableView, tablePoints, tableNumber } from '@/components/vault/tables';

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
