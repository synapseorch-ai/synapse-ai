import { PAGE_GUTTER, Spinner } from '@/components/ui';
import { cn } from '@/lib/cn';

/*
 * Between two settings tabs: the page frame's geometry (M1a) — the eyebrow, a
 * bar where the title will be, the column and the body — so nothing jumps when
 * the tab lands. Deliberately no <h1>: checks and screen readers wait for the
 * real title, and a placeholder heading would be read as the page's name.
 */
export default function Loading() {
    return (
        <div className="flex h-full min-h-0 flex-col" aria-busy="true">
            {/* The frame's own gutter (F897), so the skeleton lands where the page will. */}
            <div className={cn('shrink-0 pt-5 md:pt-8', PAGE_GUTTER)}>
                <div className="mb-1 flex h-5 items-center text-xs font-medium text-text-faint">Settings</div>
                <div className="h-8 w-48 animate-pulse rounded-md bg-surface-2" />
                <div className="mt-2.5 h-4 w-80 max-w-full animate-pulse rounded-md bg-surface" />
            </div>
            <div className={cn('min-h-0 flex-1 overflow-y-auto pb-12 pt-6 md:pt-8', PAGE_GUTTER)}>
                <div className="space-y-6 lg:ml-[15.5rem] lg:max-w-5xl">
                    <div className="h-14 animate-pulse rounded-md bg-surface" />
                    <div className="h-14 animate-pulse rounded-md bg-surface" />
                    <div className="h-14 animate-pulse rounded-md bg-surface" />
                    <div className="flex items-center justify-center py-16">
                        <Spinner size={24} className="text-text-faint" />
                    </div>
                </div>
            </div>
        </div>
    );
}
