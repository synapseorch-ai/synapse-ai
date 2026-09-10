import { BACKEND_URL, backendHeaders } from '@/lib/backend';

import WidgetChat, { type Theme } from '../../[key]/WidgetChat';
import styles from '../../[key]/embed.module.css';

export const dynamic = 'force-dynamic';

/**
 * The owner's own preview of their widget.
 *
 * **The same component the visitor gets**, deliberately. A mock would be quicker
 * to build and would start drifting the first time the real one changed — and a
 * preview that lies is worse than no preview, because it is believed.
 *
 * What differs is only how it is let in. There is no parent origin to check
 * here: the caller is the workspace itself, already behind the login gate, so
 * `/embed/preview/` is **excluded** from the auth bypass that lets a stranger
 * reach `/embed/{key}`. Getting that exclusion wrong is how a preview becomes a
 * way to talk to any widget without being on its allowed list, so it is asserted
 * in `test_widget_routes.py`.
 *
 * `frame-ancestors 'self'` is set for this path in `proxy.ts`: the settings
 * screen has to be able to frame it, and nobody else does.
 */

type PageProps = { params: Promise<{ id: string }> };

export default async function PreviewPage({ params }: PageProps) {
  const { id } = await params;

  let session: { token?: string; widget?: Theme; detail?: string } = {};
  let status = 500;
  try {
    const response = await fetch(
      `${BACKEND_URL}/api/widgets/${encodeURIComponent(id)}/preview`,
      { method: 'POST', headers: backendHeaders(), cache: 'no-store' },
    );
    status = response.status;
    session = await response.json();
  } catch {
    session = { detail: 'Could not load the preview.' };
  }

  if (status !== 200 || !session.token || !session.widget) {
    return (
      <main className={styles.refusal} role="alert">
        <p className={styles.refusalTitle}>No preview yet.</p>
        <p className={styles.refusalBody}>
          {session.detail ?? 'Pick an agent for this widget and it will appear here.'}
        </p>
      </main>
    );
  }

  return (
    <WidgetChat token={session.token} storageKey={`preview:${id}`} theme={session.widget} preview />
  );
}
