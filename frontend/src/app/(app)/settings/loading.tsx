import { Spinner } from '@/components/ui';

/*
 * Between two settings tabs: the page frame's geometry (M1a) — the eyebrow, a
 * bar where the title will be, the column and the body — so nothing jumps when
 * the tab lands. Deliberately no <h1>: checks and screen readers wait for the
 * real title, and a placeholder heading would be read as the page's name.
 */
export default function Loading() {
    return (
        <div className="flex h-full min-h-0 flex-col" aria-busy="true">
            <div className="shrink-0 px-4 pt-5 md:px-10 md:pt-8">
                <div className="mb-1 flex h-5 items-center text-xs font-medium text-text-faint">Settings</div>
                <div className="h-8 w-48 animate-pulse rounded-md bg-surface-2" />
                <div className="mt-2.5 h-4 w-80 max-w-full animate-pulse rounded-md bg-surface" />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-12 pt-6 md:px-10 md:pt-8">
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
