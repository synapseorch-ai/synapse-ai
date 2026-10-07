/*
 * One of Home's sections (M2a): a head and a link on one line, then the body.
 * No box — UX1's "boxes inside boxes" finding — the space is the divider.
 */
import type { ReactNode } from 'react';

import { sectionClass, TextLink } from '@/components/ui';
import { cn } from '@/lib/cn';

export function HomeSection({ id, title, link, aside, children, className }: {
    /** For the heading's id, which names the section. */
    id: string;
    title: ReactNode;
    link?: { href: string; label: string };
    /** Anything else on the head's line, before the link. */
    aside?: ReactNode;
    children: ReactNode;
    className?: string;
}) {
    return (
        <section aria-labelledby={`home-${id}`} className={cn('grid min-w-0 content-start gap-2.5', className)}>
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                <h2 id={`home-${id}`} className={sectionClass}>{title}</h2>
                {(aside || link) && (
                    <span className="flex items-baseline gap-3 text-[13px] font-medium">
                        {aside}
                        {link && <TextLink href={link.href}>{link.label}</TextLink>}
                    </span>
                )}
            </div>
            {children}
        </section>
    );
}
