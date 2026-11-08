import { notFound } from 'next/navigation';

import { kitchenSinkEnabled } from '@/lib/kitchen-sink';

import { KitchenSink } from './KitchenSink';

/**
 * The design-system review page, ported from the cloud console alongside the
 * primitives themselves. One screen showing every component in both themes —
 * the fastest way to judge the kit, and the regression surface for everything
 * built on it.
 *
 * Gated on the server, where the real environment is visible.
 */
export const dynamic = 'force-dynamic';

export default function KitchenSinkPage() {
    if (!kitchenSinkEnabled()) notFound();
    return <KitchenSink />;
}
