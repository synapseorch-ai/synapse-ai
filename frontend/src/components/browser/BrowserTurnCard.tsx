'use client';

import { useState } from 'react';
import { Globe } from 'lucide-react';

import { Button } from '@/components/ui';

import { bannerKindOf, type BannerKind } from './model';

const LEADS: Record<BannerKind, string> = {
  send: 'Your turn: check it and send it yourself',
  sensitive: 'Your turn in the browser',
  captcha: 'Your turn: a check that you’re a person',
  asked: 'Your turn in the browser',
  session_expired: 'Your turn: sign in again',
  allow_typing: 'Your turn: may it type on this site?',
  press: 'Your turn: press it yourself',
};

/** What became of a turn nobody handed back. */
const ENDED: Record<'moved_on' | 'closed', string> = {
  moved_on: 'You moved on — this was not done in the browser',
  closed: 'The browser closed before this was done',
};

/**
 * A browser turn where the conversation or the run is read (BRW2, F1077): the agent handed the page to the person,
 * for something only they should do (a password, a card, a code, a check you're a person, a send). The same card as
 * the strip in the pane (Naveen: act where you read). It opens the browser, and Done hands the wheel back from here.
 * A turn nobody handed back says what became of it instead, as the approval card's "You moved on" does.
 */
export function BrowserTurnCard({ reason, kind, answered, ended, onOpen, onDone }: {
  reason: string;
  kind?: BannerKind;
  /** Handed back already: the card stays as a record. */
  answered?: boolean;
  /**
   * Not handed back, and no longer asked: the person wrote instead (`moved_on`), or the browser it was for is gone
   * (`closed`, a reopened chat's card nothing waits on). A record, with no buttons.
   */
  ended?: 'moved_on' | 'closed';
  onOpen?: () => void;
  /** Hand the wheel back. Resolves false when it could not. */
  onDone?: () => Promise<boolean>;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  // An old component's `login_expired` reads as the session that expired it is (F1138).
  const shown = bannerKindOf(kind) ?? 'asked';
  const lead = LEADS[shown];
  const secret = shown === 'sensitive' || shown === 'session_expired';
  const over = answered ? 'You handed the browser back' : ended ? ENDED[ended] : null;
  return (
    <div data-browser-turn className="flex min-w-0 flex-col gap-2.5 rounded-xl border border-person/60 bg-person-subtle px-4 py-3.5">
      <span className="inline-flex items-center gap-1.5 text-xs font-medium text-person-ink">
        <Globe className="size-3.5" aria-hidden />
        {over ?? lead}
      </span>
      <p className="text-[15px] leading-relaxed text-text">
        {reason}
        {secret && !over && (
          <span className="text-text-muted"> The agent can&apos;t see what you type, and nothing is recorded while you drive.</span>
        )}
      </p>
      {!over && (
        <div className="flex flex-wrap items-center gap-2">
          {onOpen && <Button variant="person" size="sm" onClick={onOpen}>Open the browser</Button>}
          {onDone && (
            <Button
              variant="secondary"
              size="sm"
              loading={busy}
              onClick={async () => {
                setBusy(true);
                setFailed(false);
                const ok = await onDone().catch(() => false);
                setBusy(false);
                if (!ok) setFailed(true);
              }}
            >
              Done
            </Button>
          )}
          {failed && <span className="text-xs text-danger">The browser could not be handed back. Try again.</span>}
        </div>
      )}
    </div>
  );
}
