/**
 * What kind of file a Vault file is, by its extension (F1207): the one rule both products' Vault screens open a file
 * by. Synced to cloud's web with the rest of `components/vault`.
 */
import type { CodeLanguage } from '@/components/ui';

export type FileKind = 'markdown' | 'json' | 'table' | 'code' | 'image' | 'pdf';

/** Shown in the page by `/api/vault/raw` (its `_INLINE_TYPES`). */
const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif']);
const TABLE_EXTS = new Set(['.csv', '.tsv']);
const MARKDOWN_EXTS = new Set(['.md', '.markdown', '.mdx']);

/** A file's kind by its extension. A text file of any other kind is `code`: an editor with line numbers. */
export function fileKind(ext: string | undefined): FileKind {
    const e = dotted(ext);
    if (MARKDOWN_EXTS.has(e)) return 'markdown';
    if (e === '.json') return 'json';
    if (TABLE_EXTS.has(e)) return 'table';
    if (IMAGE_EXTS.has(e)) return 'image';
    if (e === '.pdf') return 'pdf';
    return 'code';
}

/** The editor's colouring for a code kind: the languages the product's editor has. */
export function codeLanguage(ext: string | undefined): CodeLanguage {
    return dotted(ext) === '.py' ? 'python' : 'text';
}

/** An extension with its dot, lower case: self-hosted's tree has `.csv`, cloud's `csv`. */
function dotted(ext: string | undefined): string {
    const e = (ext ?? '').toLowerCase();
    return e && !e.startsWith('.') ? `.${e}` : e;
}
