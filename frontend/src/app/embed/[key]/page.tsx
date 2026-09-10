import { headers } from 'next/headers';

import { BACKEND_URL, backendHeaders } from '@/lib/backend';

import EmbedRefusal from './EmbedRefusal';
import WidgetChat from './WidgetChat';

export const dynamic = 'force-dynamic';

/**
 * The page inside the iframe. This is where a widget is allowed, or is not.
 *
 * Two things happen here and nowhere else:
 *
 * 1. **The parent origin is checked** against the widget's allow-list, and the
 *    visitor token is minted only if it passes. The origin comes from `Referer`,
 *    which the browser sets on a subframe navigation and the embedding page
 *    cannot forge from script. It is deliberately *not* read from a query
 *    parameter — the host page controls its own URL, so a parameter would be the
 *    embedding site vouching for itself.
 *
 * 2. **`frame-ancestors` is set** from the same list, so the browser refuses to
 *    paint this page inside a site that is not on it. That check is the strong
 *    one: it is enforced by the visitor's browser rather than by us, so it holds
 *    even against a request that reached here some other way.
 *
 * Neither is sufficient alone — a forged `Referer` defeats the first, a browser
 * with CSP off defeats the second — and running both is why a scraped key is
 * worth so little.
 *
 * A refusal *renders*. It does not 404 and it does not return an empty stream:
 * the person who sees it is nearly always the customer who has just pasted the
 * snippet onto a site they forgot to list, and the fix takes them ten seconds
 * once they can read what happened.
 */

type PageProps = { params: Promise<{ key: string }> };

function parentOrigin(referer: string | null): string {
  if (!referer) return '';
  try {
    return new URL(referer).origin;
  } catch {
    return '';
  }
}

export default async function EmbedPage({ params }: PageProps) {
  const { key } = await params;
  const incoming = await headers();
  const origin = parentOrigin(incoming.get('referer'));

  let session: {
    token?: string;
    session_id?: string;
    widget?: { greeting?: string; accent?: string; title?: string; launcher_label?: string };
    detail?: string;
  } = {};
  let status = 500;

  try {
    const response = await fetch(`${BACKEND_URL}/api/widget/session`, {
      method: 'POST',
      headers: backendHeaders(),
      body: JSON.stringify({
        public_key: key,
        // An empty origin — a direct visit with no referrer — is passed through
        // as-is and refused by the allow-list, rather than being papered over
        // with a default. "Opened outside a page" is not an origin.
        origin: origin || 'null',
      }),
      cache: 'no-store',
    });
    status = response.status;
    session = await response.json();
  } catch {
    session = { detail: 'This chat is unavailable right now.' };
  }

  if (status !== 200 || !session.token || !session.session_id) {
    return <EmbedRefusal detail={session.detail ?? 'This chat is unavailable right now.'} />;
  }

  return (
    <WidgetChat
      token={session.token}
      storageKey={key}
      greeting={session.widget?.greeting ?? 'Hi! How can I help?'}
      accent={session.widget?.accent ?? '#4f46e5'}
      title={session.widget?.title ?? ''}
    />
  );
}
