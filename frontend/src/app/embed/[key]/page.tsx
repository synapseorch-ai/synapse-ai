import { headers } from 'next/headers';

import { BACKEND_URL, sessionHeaders } from '@/lib/backend';

import EmbedRefusal from './EmbedRefusal';
import WidgetChat, { type Theme } from './WidgetChat';

export const dynamic = 'force-dynamic';

/**
 * The page inside the iframe. This is where a widget is allowed, or is not.
 *
 * Two things happen here and nowhere else:
 *
 * 1. **The parent origin is checked** against the widget's allow-list, and the
 *    visitor token is minted only if it passes. The origin comes from `Referer`,
 *    which the browser sets on a subframe navigation and the embedding page
 *    cannot forge from script. v1.js's frame asks for it with its own referrer
 *    policy, `origin`, so a page that sends no referrer still names its origin
 *    and nothing more (F1522). It is deliberately *not* read from a query
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

/** What the visitor reads when the browser named no page at all (F1522), never "null is not an allowed origin". */
const NO_ADDRESS =
  'The browser sent no address for the page this chat is on, so the widget can’t check that it’s allowed there.';

/** The embedding page's origin from the frame's `Referer`; '' when there is none, or only an opaque one ("null"). */
function parentOrigin(referer: string | null): string {
  if (!referer) return '';
  try {
    const origin = new URL(referer).origin;
    return origin === 'null' ? '' : origin;
  } catch {
    return '';
  }
}

export default async function EmbedPage({ params }: PageProps) {
  const { key } = await params;
  const incoming = await headers();
  const origin = parentOrigin(incoming.get('referer'));

  // No address at all: a browser that ignored the frame's own referrer policy, or this page opened by itself. The
  // allow-list is never asked about an origin nobody named; the visitor is told what happened, in words (F1522).
  if (!origin) {
    return <EmbedRefusal detail={NO_ADDRESS} />;
  }

  let session: {
    token?: string;
    session_id?: string;
    // The whole theme, built server-side by `branding.public_theme` so the
    // widget and its preview render from one shape and cannot disagree.
    widget?: Theme;
    detail?: string;
  } = {};
  let status = 500;

  try {
    const response = await fetch(`${BACKEND_URL}/api/widget/session`, {
      method: 'POST',
      headers: await sessionHeaders(),
      body: JSON.stringify({
        public_key: key,
        // Always an origin the browser named: a page that named none was refused
        // above, never papered over with a default.
        origin,
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

  return <WidgetChat token={session.token} storageKey={key} theme={session.widget!} />;
}
