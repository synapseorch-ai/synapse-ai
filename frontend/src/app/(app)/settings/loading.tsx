import { Spinner } from '@/components/ui';

export default function Loading() {
    return (
        <div className="flex h-full min-h-0 flex-col">
            <div className="shrink-0 border-b border-border px-6 py-4">
                <div className="h-5 w-40 animate-pulse rounded-md bg-surface-2" />
                <div className="mt-2 h-3 w-72 animate-pulse rounded-md bg-surface" />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-6 md:p-12">
                <div className="mx-auto max-w-5xl space-y-6">
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
