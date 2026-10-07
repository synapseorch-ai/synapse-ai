'use client';
/*
 * Templates: the example packs, under Workflows (D209, M1a).
 *
 * OSS has no template gallery of its own; its ready-made starting points are
 * the example packs that Settings → Import / Export → Examples has always
 * listed. This page shows the same list and the same "Preview & Import" flow
 * (a pack opens in the import preview, on this page). Import / Export keeps its
 * Examples view too, so nothing that was there has moved away.
 */
import { useCallback, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useDispatch } from 'react-redux';
import { ArrowLeft } from 'lucide-react';

import type { AppDispatch } from '@/store';
import { fetchAllSettingsData } from '@/store/settingsSlice';
import { navEntryFor } from '@/lib/nav';
import { ExamplesView } from '@/components/settings/import-export/ExamplesView';
import { ImportView } from '@/components/settings/import-export/ImportView';

export function TemplatesScreen() {
    const router = useRouter();
    const dispatch = useDispatch<AppDispatch>();
    // The pack being previewed, as Import / Export hands it from Examples to Import.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const [bundle, setBundle] = useState<any>(null);
    const back = useCallback(() => setBundle(null), []);

    if (!bundle) return <ExamplesView onLoadBundle={setBundle} />;

    return (
        <div className="space-y-6">
            <button
                type="button"
                onClick={back}
                className="inline-flex items-center gap-1.5 text-sm text-text-muted transition-colors hover:text-text"
            >
                <ArrowLeft className="size-4" aria-hidden />
                All templates
            </button>
            <ImportView
                preloadedBundle={bundle}
                onReset={back}
                onImportSuccess={() => dispatch(fetchAllSettingsData())}
                onNavigate={(tab) => router.push(navEntryFor(tab)?.href ?? `/settings/${tab}`)}
            />
        </div>
    );
}
