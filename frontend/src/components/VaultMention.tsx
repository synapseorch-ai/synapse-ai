'use client';

/**
 * The vault's `@`, for the kit's editors.
 *
 * A mention source: the markdown editor's suggestion list calls `search` as you
 * type after `@` and inserts `@[path]`. It replaced `VaultTextarea`, a textarea
 * with its own dropdown that every prompt field wore; the editors now share one
 * list for `{{` and `@`. Cloud's copy of this file exports no source until it has
 * a vault.
 */

import type { MentionSource } from '@/components/ui';

interface VaultFile {
    name: string;
    path: string;
    ext: string;
}

export const vaultMentions: MentionSource = {
    trigger: '@',
    search: async (query) => {
        const response = await fetch(`/api/vault/search?q=${encodeURIComponent(query)}`);
        if (!response.ok) return [];
        const data = await response.json();
        const files: VaultFile[] = Array.isArray(data.files) ? data.files : [];
        return files.map((file) => ({ id: file.path, label: file.path, detail: file.ext, insert: `@[${file.path}]` }));
    },
};
