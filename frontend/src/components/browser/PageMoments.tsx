'use client';

import { useState } from 'react';
import { CreditCard, Lock, MousePointerClick, Send, ShieldCheck } from 'lucide-react';

import { Button, Input } from '@/components/ui';
import { cn } from '@/lib/cn';

import { hostOf, type BrowserBanner, type PageDialog, type PageFile, type PageSelect } from './model';

/**
 * What the person is asked to handle, as an amber strip under the address with its action in it (BRW2, F1077;
 * Naveen's pick A of the co-drive prototype). A send the agent tried is held at the network and named, with
 * "It only loaded data" for sites that send to read. A turn the agent handed over (a password, a code, a check that
 * you're a person, a login that ended, or something it asked) ends with Done, which hands the wheel back. Typing on
 * a site no one listed is allowed here, once a session. A final button the agent stopped at (`press`) is named with
 * what it does, for the person to press; money is always theirs, so a held payment is never let through, only taken
 * over (Naveen 2026-10-28).
 */
export function MomentStrip({ banner, agentName, onDone, onRelease, onAllow, onTakeOver, failed }: {
  banner: BrowserBanner;
  agentName: string;
  /** Hands the wheel back; absent while the agent still holds it. */
  onDone?: () => void;
  onRelease?: () => void;
  onAllow?: (host: string) => void;
  /** Takes the wheel, so the person can press the page's button; absent while they hold it. */
  onTakeOver?: () => void;
  /** Why the last move of the wheel did not happen. */
  failed?: string | null;
}) {
  const money = banner.press?.tier === 'money' || !!banner.held?.money;
  const Icon = money ? CreditCard : banner.kind === 'send' ? Send : banner.kind === 'press' ? MousePointerClick
    : banner.kind === 'captcha' ? ShieldCheck : Lock;
  const reason = banner.reason?.trim();
  let words: React.ReactNode;
  if (banner.kind === 'send') {
    const site = hostOf(banner.held?.url ?? '') || 'the site';
    const button = <>the page&apos;s own {reason ? <>“{reason}”</> : 'button'}</>;
    words = money ? (
      <><strong className="font-semibold">Your turn: payments are always yours.</strong>{' '}
        {agentName} tried to pay on <span translate="no">{site}</span>. Nothing has gone. If it&apos;s right, take
        over and press {button} yourself.</>
    ) : (
      <><strong className="font-semibold">Your turn: the send is yours.</strong>{' '}
        {agentName} tried to send this to <span translate="no">{site}</span>. Nothing has gone. If it&apos;s right,
        press {button} yourself.</>
    );
  } else if (banner.kind === 'press' && banner.press) {
    const { label, does } = banner.press;
    words = (
      <><strong className="font-semibold">Your turn. Press ‘{label}’ yourself.</strong>{' '}
        It {does.replace(/\.$/, '')}.{' '}
        {money
          ? 'Payments are always yours: an agent never presses them.'
          : `${agentName} presses a button like this only when you’ve asked it to in your own words.`}
        {reason ? <> {agentName} was going to: {reason.replace(/\.$/, '')}.</> : null}</>
    );
  } else if (banner.kind === 'allow_typing') {
    words = (
      <><strong className="font-semibold">{agentName} wants to type on <span translate="no">{banner.host}</span>.</strong>
        {reason ? <> {reason}.</> : null} It can read any site, but types only where you allow it.</>
    );
  } else {
    const fallback = { sensitive: 'this field is yours', captcha: 'the site wants to check you’re a person',
                       asked: 'the agent needs you here', login_expired: 'log in again',
                       press: 'press the page’s button yourself' }[banner.kind];
    words = (
      <><strong className="font-semibold">Your turn:</strong> {reason || fallback}.
        {banner.kind === 'sensitive' || banner.kind === 'login_expired'
          ? ' The agent can’t see what you type, and nothing is recorded while you drive.' : ''}</>
    );
  }
  // A held payment is never let through: `/release` answers 409 for it.
  const release = banner.kind === 'send' && banner.held && !money ? onRelease : undefined;
  return (
    <div role="status" className="flex shrink-0 flex-col gap-2 border-b border-person bg-person-subtle px-3 py-2.5 text-xs text-person-fg">
      <div className="flex items-start gap-2.5">
        <Icon className="mt-0.5 size-4 shrink-0 text-person-ink" aria-hidden />
        <p className="min-w-0 flex-1 leading-relaxed">{words}</p>
        {onDone && banner.kind !== 'allow_typing' && <Button variant="person" size="sm" onClick={onDone}>Done</Button>}
        {!onDone && onTakeOver && banner.kind !== 'allow_typing' && (
          <Button variant="person" size="sm" onClick={onTakeOver}>Take over</Button>
        )}
      </div>
      {release || (banner.kind === 'allow_typing' && banner.host && onAllow) ? (
        <div className="flex flex-wrap gap-2 pl-6">
          {release && (
            <Button variant="secondary" size="sm" onClick={release}>It only loaded data: let the agent carry on</Button>
          )}
          {banner.kind === 'allow_typing' && banner.host && onAllow && (
            <Button variant="person" size="sm" onClick={() => onAllow(banner.host!)}>Allow typing on {banner.host}</Button>
          )}
        </div>
      ) : null}
      {failed && <p className="pl-6 text-danger">{failed}</p>}
    </div>
  );
}

/** A card of ours over the page, for what the page itself opened outside its own drawing. */
function Ask({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="absolute inset-0 flex items-center justify-center bg-scrim/60">
      <div role="dialog" aria-label={label}
           className="flex max-h-[calc(100%-2rem)] w-80 max-w-[calc(100%-2rem)] flex-col gap-3 overflow-y-auto rounded-xl bg-bg p-4 shadow-elevated">
        {children}
      </div>
    </div>
  );
}

/** The page's alert, confirm or prompt (DESIGN §2.3's screencast gaps), answered by the person while they drive. */
export function DialogAsk({ dialog, onAnswer }: { dialog: PageDialog; onAnswer: (accept: boolean, text?: string) => void }) {
  const [text, setText] = useState(dialog.default ?? '');
  const prompt = dialog.type === 'prompt';
  return (
    <Ask label="The page asks">
      <p className="text-xs font-medium text-text-faint">The page asks</p>
      <p className="whitespace-pre-wrap break-words text-sm text-text">{dialog.message}</p>
      {prompt && <Input value={text} onChange={e => setText(e.target.value)} aria-label="Your answer" autoFocus />}
      <div className="flex justify-end gap-2">
        {dialog.type !== 'alert' && (
          <Button variant="secondary" size="sm" onClick={() => onAnswer(false)}>
            {dialog.type === 'beforeunload' ? 'Stay' : 'Cancel'}
          </Button>
        )}
        <Button size="sm" onClick={() => onAnswer(true, prompt ? text : undefined)} autoFocus={!prompt}>
          {dialog.type === 'beforeunload' ? 'Leave' : 'OK'}
        </Button>
      </div>
    </Ask>
  );
}

/** A native dropdown the page opened: our own list of its options. */
export function SelectAsk({ select, onChoose }: { select: PageSelect; onChoose: (value: string) => void }) {
  return (
    <Ask label={`Choose: ${select.target}`}>
      <p className="text-xs font-medium text-text-faint">Choose: {select.target}</p>
      <div role="listbox" aria-label={select.target} className="flex flex-col gap-1">
        {select.options.map(o => (
          <button
            key={o.value}
            type="button"
            role="option"
            aria-selected={o.value === select.selected}
            onClick={() => onChoose(o.value)}
            className={cn('rounded-md px-3 py-2 text-left text-sm outline-none hover:bg-row-hover focus-visible:ring-2 focus-visible:ring-ring',
              o.value === select.selected ? 'bg-accent-subtle text-accent-ink' : 'text-text')}
          >
            {o.label}
          </button>
        ))}
      </div>
    </Ask>
  );
}

/** The most a page is handed in one file here (BRW1's `/file`). */
const FILE_LIMIT = 25 * 1024 * 1024;

/** A file picker the page opened: the person picks from their own computer, and it fills the page's chooser. */
export function FileAsk({ file, onOffer, onCancel }: {
  file: PageFile;
  onOffer: (picked: File) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Ask label="The page wants a file">
      <p className="text-xs font-medium text-text-faint">The page wants {file.multiple ? 'files' : 'a file'}</p>
      <label className="flex flex-col gap-2 text-sm text-text">
        Choose a file from your computer
        <input
          type="file"
          accept={file.accept || undefined}
          disabled={busy}
          onChange={async (e) => {
            const picked = e.currentTarget.files?.[0];
            if (!picked) return;
            if (picked.size > FILE_LIMIT) {
              setFailed('That file is over 25 MB, the most a page is handed here.');
              return;
            }
            setBusy(true);
            setFailed(null);
            const ok = await onOffer(picked).catch(() => false);
            setBusy(false);
            if (!ok) setFailed('The page did not take the file. Try again.');
          }}
          className="text-xs text-text-muted file:mr-3 file:rounded-md file:border file:border-border file:bg-surface file:px-3 file:py-1.5 file:text-xs file:text-text"
        />
      </label>
      {failed && <p role="alert" className="text-xs text-danger">{failed}</p>}
      <div className="flex justify-end">
        <Button variant="secondary" size="sm" onClick={onCancel}>Not now</Button>
      </div>
    </Ask>
  );
}
