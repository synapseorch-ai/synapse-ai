'use client';

import { useEffect } from 'react';

import styles from './embed.module.css';

/**
 * Shown when the widget resolved but may not run here — an unlisted origin, a
 * switched-off widget, an agent that has gone away.
 *
 * A client component only so it can announce itself to the loader. Reaching this
 * page at all means the browser *did* paint the frame, so the reason is on
 * screen and the loader should stay quiet rather than adding a second, vaguer
 * explanation to the console.
 */
export default function EmbedRefusal({ detail }: { detail: string }) {
  useEffect(() => {
    window.parent?.postMessage({ type: 'synapse:ready', refused: true }, '*');
  }, []);

  return (
    <main className={styles.refusal} role="alert">
      <p className={styles.refusalTitle}>This chat can’t load here.</p>
      <p className={styles.refusalBody}>{detail}</p>
    </main>
  );
}
