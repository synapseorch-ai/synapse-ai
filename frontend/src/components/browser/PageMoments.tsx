'use client';

import { useEffect, useState } from 'react';
import { Lock, Send, ShieldCheck } from 'lucide-react';

import { Button, Input } from '@/components/ui';
import { cn } from '@/lib/cn';

import { hostOf, type BrowserBanner, type PageDialog, type PageFile, type PageSelect } from './model';

/**
 * What the person is asked to handle, as an amber strip under the address with its action in it (BRW2, F1077;
 * Naveen's pick A of the co-drive prototype). A send the agent tried is held at the network and named, with
 * "It only loaded data" for sites that send to read. A turn the agent handed over (a password, a code, a check that
 * you're a person, a login that ended, or something it asked) ends with Done, which hands the wheel back. Typing on
 * a site no one listed is allowed here, once a session.
 */
export function MomentStrip({ banner, agentName, onDone, onRelease, onAllow }: {
  banner: BrowserBanner;
  agentName: string;
  /** Hands the wheel back; absent while the agent still holds it. */
  onDone?: () => void;
  onRelease?: () => void;
  onAllow?: (host: string) => void;
}) {
  const Icon = banner.kind === 'send' ? Send : banner.kind === 'captcha' ? ShieldCheck : Lock;
  const reason = banner.reason?.trim();
  let words: React.ReactNode;
  if (banner.kind === 'send') {
    const site = hostOf(banner.held?.url ?? '') || 'the site';
    words = (
      <><strong className="font-semibold">Your turn: the send is yours.</strong>{' '}
        {agentName} tried to send this to <span translate="no">{site}</span>. Nothing has gone. If it&apos;s right,
        press the page&apos;s own {reason ? <>“{reason}”</> : 'button'} yourself.</>
    );
  } else if (banner.kind === 'allow_typing') {
    words = (
      <><strong className="font-semibold">{agentName} wants to type on <span translate="no">{banner.host}</span>.</strong>
        {reason ? <> {reason}.</> : null} It can read any site, but types only where you allow it.</>
    );
  } else {
    const fallback = { sensitive: 'this field is yours', captcha: 'the site wants to check you’re a person',
                       asked: 'the agent needs you here', login_expired: 'log in again' }[banner.kind];
    words = (
      <><strong className="font-semibold">Your turn:</strong> {reason || fallback}.
        {banner.kind === 'sensitive' || banner.kind === 'login_expired'
          ? ' The agent can’t see what you type, and nothing is recorded while you drive.' : ''}</>
    );
  }
  return (
    <div role="status" className="flex shrink-0 flex-col gap-2 border-b border-person bg-person-subtle px-3 py-2.5 text-xs text-person-fg">
      <div className="flex items-start gap-2.5">
        <Icon className="mt-0.5 size-4 shrink-0 text-person-ink" aria-hidden />
        <p className="min-w-0 flex-1 leading-relaxed">{words}</p>
        {onDone && banner.kind !== 'allow_typing' && <Button variant="person" size="sm" onClick={onDone}>Done</Button>}
      </div>
      {(banner.kind === 'send' && banner.held && onRelease) || (banner.kind === 'allow_typing' && banner.host && onAllow) ? (
        <div className="flex flex-wrap gap-2 pl-6">
          {banner.kind === 'send' && onRelease && (
            <Button variant="secondary" size="sm" onClick={onRelease}>It only loaded data: let the agent carry on</Button>
          )}
          {banner.kind === 'allow_typing' && banner.host && onAllow && (
            <Button variant="person" size="sm" onClick={() => onAllow(banner.host!)}>Allow typing on {banner.host}</Button>
          )}
        </div>
      ) : null}
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

/** A file picker the page opened: the files this browser may hand it, never the person's disk. */
export function FileAsk({ file, files, onOffer }: {
  file: PageFile;
  /** Read when the card opens; absent when this product offers none here. */
  files?: () => Promise<{ id: string; name: string }[]>;
  onOffer: (attachmentId: string) => void;
}) {
  const [list, setList] = useState<{ id: string; name: string }[] | null>(files ? null : []);
  useEffect(() => {
    if (!files) return;
    let live = true;
    files().then(l => { if (live) setList(l); }).catch(() => { if (live) setList([]); });
    return () => { live = false; };
  }, [files]);
  return (
    <Ask label="The page wants a file">
      <p className="text-xs font-medium text-text-faint">The page wants {file.multiple ? 'files' : 'a file'}</p>
      {list === null ? (
        <p className="text-sm text-text-muted">Reading the files…</p>
      ) : list.length === 0 ? (
        <p className="text-sm text-text-muted">No files to offer here. A page can be given the files of the chat or run it works for.</p>
      ) : (
        <div className="flex flex-col gap-1">
          {list.map(f => (
            <button key={f.id} type="button" onClick={() => onOffer(f.id)}
                    className="truncate rounded-md px-3 py-2 text-left text-sm text-text hover:bg-row-hover">
              {f.name}
            </button>
          ))}
        </div>
      )}
    </Ask>
  );
}
