"use client";

/*
 * Home's "More you can do" (GUIDE1, D318, F914, F917): what this workspace hasn't tried yet, each in a line and a
 * link. A card goes once the thing is used, and the row can be put away. No "new" badge: everything here simply is
 * the product (the launch rule). Each product decides what counts as used (`moreToTry`).
 */
import { ArrowRight, X } from 'lucide-react';
import Link from 'next/link';

import { IconButton } from '@/components/ui';

import { HomeSection } from './HomeSection';
import { MORE_TO_TRY, type TryKey } from './home';

export function MoreToTry({ keys, onDismiss }: { keys: TryKey[]; onDismiss: () => void }) {
    if (keys.length === 0) return null;
    return (
        <div data-tour="home-more">
            <HomeSection
                id="more"
                title="More you can do"
                aside={<IconButton label="Hide “More you can do”" icon={X} size="sm" onClick={onDismiss} />}
            >
                <ul className="m-0 grid list-none gap-3 p-0 sm:grid-cols-2 xl:grid-cols-4">
                    {keys.map(key => {
                        const item = MORE_TO_TRY[key];
                        return (
                            <li key={key}>
                                <Link
                                    href={item.href}
                                    className="group flex h-full flex-col gap-1.5 rounded-lg border border-border bg-surface px-4 py-3.5 transition-colors hover:border-border-strong"
                                >
                                    <span className="flex items-center justify-between gap-2 text-sm font-medium text-text">
                                        {item.title}
                                        <ArrowRight className="size-4 shrink-0 text-text-faint transition-transform group-hover:translate-x-0.5" aria-hidden />
                                    </span>
                                    <span className="text-xs leading-relaxed text-text-muted">{item.line}</span>
                                </Link>
                            </li>
                        );
                    })}
                </ul>
            </HomeSection>
        </div>
    );
}
