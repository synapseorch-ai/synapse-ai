'use client';

/*
 * Add a number (V30): one modal, three steps. Your Twilio account (a key,
 * checked straight away by listing its numbers), the number (where each points
 * now), and who answers. Then the number opens.
 *
 * The key is a Restricted or Standard API key, never the account's auth token
 * (V4). Both paste into the same fields; step one asks which kind, because that
 * is the trade-off a person makes (Naveen, VOX1c): a Restricted key holds only
 * what calls need but Twilio won't sign its requests, a Standard key signs them
 * but can do almost anything. The choice only changes the instructions; the API
 * finds out what the key may do. A key that cannot change numbers connects with
 * the address to paste into Twilio (V7).
 *
 * The steps read as a path (Naveen, VOX1c): a stepper over the body, done steps
 * ticked and one click back, and step one's instructions numbered.
 */
import { ArrowLeft, Check, ExternalLink } from 'lucide-react';
import { useState } from 'react';

import {
    Badge, Button, Combobox, ErrorNote, Field, Input, Modal, Segmented, Select, Textarea,
} from '@/components/ui';
import { cn } from '@/lib/cn';

import { usePhoneHost } from './host';
import {
    errorWords,
    DEFAULT_NOTICE, NOTICE_HINT, E164, RESTRICTED_PERMISSIONS, TWILIO_KEYS_URL, UNSET, fromChoice, settingsToSend, toChoice,
    type PhoneAgent, type PhoneNumber, type PhoneOptions, type TwilioNumber, type VoiceSettings,
} from './phone';

const STEPS = ['Twilio key', 'The number', 'Who answers'] as const;

type KeyKind = 'restricted' | 'standard';

/** The three steps as a path: done ones ticked (and a click back), the current one in the action colour. */
function Stepper({ step, onBack }: { step: number; onBack: (to: number) => void }) {
    return (
        <ol aria-label="Steps" data-phone-step={step + 1} className="m-0 grid list-none grid-cols-3 gap-3 p-0">
            {STEPS.map((label, i) => {
                const done = i < step;
                const here = i === step;
                return (
                    <li key={label} className="min-w-0">
                        <button
                            type="button"
                            disabled={!done}
                            onClick={() => onBack(i)}
                            aria-current={here ? 'step' : undefined}
                            className="grid w-full min-w-0 gap-2 text-left disabled:cursor-default"
                        >
                            <span aria-hidden className={cn('h-1 rounded-full', done || here ? 'bg-accent' : 'bg-border')} />
                            <span className={cn(
                                'flex min-w-0 items-center gap-1.5 text-xs font-medium',
                                here ? 'text-accent-ink' : done ? 'text-text' : 'text-text-faint',
                            )}>
                                <span aria-hidden className={cn(
                                    'grid size-4 shrink-0 place-items-center rounded-full text-[10px] tabular-nums',
                                    done ? 'bg-accent text-accent-fg' : here ? 'border border-accent' : 'border border-border',
                                )}>
                                    {done ? <Check className="size-3" /> : i + 1}
                                </span>
                                <span className="truncate">{label}</span>
                                {done && <span className="sr-only">, done</span>}
                            </span>
                        </button>
                    </li>
                );
            })}
        </ol>
    );
}

/** One numbered instruction of step one. */
function Instruction({ n, title, children }: { n: number; title: React.ReactNode; children?: React.ReactNode }) {
    return (
        <li className="grid grid-cols-[1.25rem_minmax(0,1fr)] gap-x-3 gap-y-2">
            <span aria-hidden className="grid size-5 place-items-center rounded-full bg-surface-2 text-xs font-medium tabular-nums text-text-muted">{n}</span>
            <span className="text-sm font-medium text-text">{title}</span>
            {children && <div className="col-start-2 grid min-w-0 gap-3">{children}</div>}
        </li>
    );
}

/** A kind of key, as a radio card: what it can do, and what that means for the calls. */
function KeyCard({ kind, picked, onPick, title, summary, recommended, children }: {
    kind: KeyKind;
    picked: boolean;
    onPick: (kind: KeyKind) => void;
    title: string;
    summary: string;
    recommended?: boolean;
    children: React.ReactNode;
}) {
    return (
        <label
            data-key-kind={kind}
            className={cn(
                'grid min-w-0 cursor-pointer grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-1 rounded-md border px-3 py-2.5',
                picked ? 'border-accent bg-accent-subtle' : 'border-border hover:bg-row-hover',
            )}
        >
            <input type="radio" name="phone-key-kind" value={kind} checked={picked} onChange={() => onPick(kind)}
                className="mt-1 accent-[var(--color-accent)]" />
            <span className="grid min-w-0 gap-0.5">
                <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-text">
                    {title}{recommended && <Badge>Recommended</Badge>}
                </span>
                <span className="text-[13px] text-text-muted">{summary}</span>
            </span>
            {picked && <div className="col-start-2 grid min-w-0 gap-1.5 text-[13px] text-text-muted">{children}</div>}
        </label>
    );
}

const BLANK_VOICE: VoiceSettings = {
    notice: '', filler: '', filler_late: '', transfer_number: '', recording: false, language: '', voice: '',
    tts_provider: '', max_minutes: null,
};

/** A number pointed at a SynapseOrch server: its address holds a secret route token, never shown. */
const OURS = '/api/messaging/twilio_voice/webhook/';

/** Where a number sends its calls now, in words. */
function pointsTo(n: TwilioNumber, takenBy: string | undefined): string {
    if (takenBy) return `Already answered here, as “${takenBy}”.`;
    if (n.voice_url.includes(OURS)) return 'Answered by SynapseOrch elsewhere (another workspace or install). Connecting it here takes its calls.';
    if (n.voice_url) return `Calls go to ${n.voice_url}. Connecting sends them to your agent instead; disconnecting puts this back.`;
    if (n.voice_application_sid) return `Calls go to a TwiML App (${n.voice_application_sid}). Connecting sends them to your agent instead.`;
    return 'Points nowhere yet.';
}

export function languageOptions(options: PhoneOptions) {
    return [
        { value: UNSET, label: "The number's own", hint: `English for its country code, else ${options.default_language}` },
        ...options.languages.map(l => ({ value: l.code, label: l.name, hint: l.code })),
    ];
}

export function AddNumber({
    open, onClose, agents, options, taken, onConnected,
}: {
    open: boolean;
    onClose: () => void;
    agents: PhoneAgent[];
    options: PhoneOptions;
    /** The numbers this workspace already answers, by Twilio's sid: shown, never offered again. */
    taken: Record<string, string>;
    onConnected: (number: PhoneNumber) => void;
}) {
    const host = usePhoneHost();
    const [step, setStep] = useState(0);
    const [kind, setKind] = useState<KeyKind>('restricted');
    const [accountSid, setAccountSid] = useState('');
    const [keySid, setKeySid] = useState('');
    const [keySecret, setKeySecret] = useState('');
    const [numbers, setNumbers] = useState<TwilioNumber[] | null>(null);
    const [picked, setPicked] = useState('');
    const [name, setName] = useState('');
    const [agentId, setAgentId] = useState('');
    const [voice, setVoice] = useState<VoiceSettings>(BLANK_VOICE);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const close = () => {
        // The secret never outlives the dialog.
        setStep(0); setKeySecret(''); setNumbers(null); setPicked(''); setError(''); setBusy(false);
        onClose();
    };
    const key = { accountSid: accountSid.trim(), keySid: keySid.trim(), keySecret };
    const keyReady = /^AC[0-9a-f]{32}$/i.test(key.accountSid) && /^SK[0-9a-f]{32}$/i.test(key.keySid) && !!keySecret;
    const number = numbers?.find(n => n.sid === picked);
    const transferBad = !!voice.transfer_number.trim() && !E164.test(voice.transfer_number.trim());

    const check = async () => {
        setBusy(true); setError('');
        try {
            const found = await host.twilioNumbers(key);
            setNumbers(found);
            const free = found.filter(n => !taken[n.sid]);
            setPicked(p => (free.some(n => n.sid === p) ? p : free[0]?.sid ?? ''));
            setStep(1);
        } catch (e) {
            setError(errorWords(e, 'Twilio did not accept the key.'));
        } finally {
            setBusy(false);
        }
    };
    const connect = async () => {
        if (!number) return;
        setBusy(true); setError('');
        try {
            const made = await host.connect({
                key, numberSid: number.sid, number: number.number, name: name.trim() || number.number, agentId,
                voice: settingsToSend({ ...voice, transfer_number: voice.transfer_number.trim() }, options),
            });
            close();
            onConnected(made);
        } catch (e) {
            setError(errorWords(e, 'The number could not be connected.'));
            setBusy(false);
        }
    };

    const back = step > 0 && (
        <Button variant="ghost" onClick={() => { setError(''); setStep(step - 1); }} disabled={busy}>
            <ArrowLeft className="size-4" aria-hidden /> Back
        </Button>
    );
    const footer = (
        <div className="flex w-full items-center justify-between gap-2">
            <span>{back}</span>
            <div className="flex gap-2">
                <Button variant="secondary" onClick={close} disabled={busy}>Cancel</Button>
                {step === 0 && <Button onClick={check} loading={busy} disabled={!keyReady}>Check and continue</Button>}
                {step === 1 && <Button onClick={() => setStep(2)} disabled={!number}>Continue</Button>}
                {step === 2 && (
                    <Button onClick={connect} loading={busy} disabled={!agentId || transferBad}>Connect number</Button>
                )}
            </div>
        </div>
    );

    return (
        <Modal
            open={open}
            onClose={close}
            size="lg"
            title="Add a number"
            footer={footer}
        >
            <div className="grid gap-5">
                <Stepper step={step} onBack={to => { setError(''); setStep(to); }} />
                {step === 0 && (
                    <ol className="m-0 grid list-none gap-5 p-0">
                        <Instruction n={1} title={
                            <a href={TWILIO_KEYS_URL} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent-ink underline-offset-2 hover:underline">
                                Open API keys in Twilio&apos;s console <ExternalLink className="size-3.5" aria-hidden />
                            </a>
                        } />
                        <Instruction n={2} title="Create a key. Which kind?">
                            <div role="radiogroup" aria-label="Kind of key" className="grid min-w-0 gap-2">
                                <KeyCard
                                    kind="restricted"
                                    picked={kind === 'restricted'}
                                    onPick={setKind}
                                    title="Restricted"
                                    recommended
                                    summary="Can do only what calls need: read and answer calls, point a number."
                                >
                                    <span>Tick these permissions (the recordings one only if you will record calls):</span>
                                    <ul className="m-0 grid min-w-0 gap-0.5 pl-4 font-code text-xs text-text [overflow-wrap:anywhere]">
                                        {RESTRICTED_PERMISSIONS.map(p => <li key={p}>{p}</li>)}
                                    </ul>
                                    <span>
                                        Twilio can&apos;t sign its requests to us with a Restricted key, so each call is
                                        checked by the secret tokens in its address instead.
                                    </span>
                                </KeyCard>
                                <KeyCard
                                    kind="standard"
                                    picked={kind === 'standard'}
                                    onPick={setKind}
                                    title="Standard"
                                    summary="Can do almost anything in your Twilio account."
                                >
                                    <span>
                                        Nothing to tick. With it, Twilio also signs every request it sends us, a check
                                        on top of the tokens. Pick it if that matters more to you than limiting what the
                                        key can do.
                                    </span>
                                </KeyCard>
                            </div>
                        </Instruction>
                        <Instruction n={3} title="Paste its SID and secret">
                            <p className="m-0 text-[13px] text-text-muted">
                                Either kind gives you a key SID and a secret. We never ask for your account&apos;s auth token.
                            </p>
                            <Field label="Account SID" hint="On your Twilio console's home page; it starts with AC.">
                                <Input id="phone-account-sid" value={accountSid} onChange={e => setAccountSid(e.target.value)}
                                    placeholder="AC…" className="font-code" autoComplete="off" spellCheck={false} />
                            </Field>
                            <Field label="API key SID" hint="The key's SID; it starts with SK.">
                                <Input id="phone-key-sid" value={keySid} onChange={e => setKeySid(e.target.value)}
                                    placeholder="SK…" className="font-code" autoComplete="off" spellCheck={false} />
                            </Field>
                            <Field label="API key secret" hint="Twilio shows it once, when you create the key. Checked with Twilio straight away, stored encrypted, never shown again.">
                                <Input id="phone-key-secret" type="password" value={keySecret} onChange={e => setKeySecret(e.target.value)}
                                    className="font-code" autoComplete="new-password" />
                            </Field>
                        </Instruction>
                    </ol>
                )}

                {step === 1 && numbers && (
                    <>
                        {numbers.length === 0 ? (
                            <div className="grid gap-2 rounded-md border border-border px-3 py-3 text-sm text-text-muted">
                                <p className="m-0 font-medium text-text">This account has no phone numbers.</p>
                                <p className="m-0">
                                    Buy one in Twilio&apos;s console, then check again. A trial account can&apos;t buy
                                    numbers. Buying a number here comes later.
                                </p>
                                <Button variant="secondary" size="sm" className="w-fit" onClick={check} loading={busy}>Check again</Button>
                            </div>
                        ) : (
                            <>
                                <p className="m-0 text-sm text-text-muted">
                                    Pick the number callers will dial. We point it at your agent, and put back where it
                                    pointed if you turn it off or delete it.
                                </p>
                                <div role="radiogroup" aria-label="Number" className="grid gap-2">
                                    {numbers.map(n => (
                                        <label
                                            key={n.sid}
                                            data-twilio-number={n.sid}
                                            className={cn(
                                                'flex items-start gap-3 rounded-md border px-3 py-2.5',
                                                taken[n.sid] ? 'cursor-default border-border opacity-70'
                                                    : picked === n.sid ? 'cursor-pointer border-accent bg-accent-subtle'
                                                        : 'cursor-pointer border-border hover:bg-row-hover',
                                            )}
                                        >
                                            <input type="radio" name="phone-number" value={n.sid} checked={picked === n.sid}
                                                disabled={!!taken[n.sid]}
                                                onChange={() => setPicked(n.sid)} className="mt-1 accent-[var(--color-accent)]" />
                                            <span className="min-w-0">
                                                <span className="block text-sm font-medium tabular-nums text-text">{n.number}</span>
                                                <span className="block break-words text-xs text-text-faint">{pointsTo(n, taken[n.sid])}</span>
                                            </span>
                                        </label>
                                    ))}
                                </div>
                                <p className="m-0 text-xs text-text-faint">Buying a number here comes later. A number bought in Twilio appears in this list.</p>
                            </>
                        )}
                    </>
                )}

                {step === 2 && (
                    <>
                        <Field composite label="Agent" hint="Answers every call to this number, as a visitor's conversation.">
                            <Combobox
                                value={agentId}
                                onChange={setAgentId}
                                options={agents.map(a => ({ value: a.id, label: a.name }))}
                                placeholder="Choose an agent"
                                searchPlaceholder="Search agents…"
                                emptyMessage="No agents yet. Make one on the Agents page first."
                                aria-label="Agent"
                            />
                        </Field>
                        <Field label="Name" hint="How this number is listed. Leave empty to use the number.">
                            <Input id="phone-name" value={name} onChange={e => setName(e.target.value)} placeholder={number?.number} />
                        </Field>
                        <Field
                            label="What callers hear first"
                            hint={NOTICE_HINT}
                        >
                            <Textarea id="phone-notice" rows={2} value={voice.notice} placeholder={DEFAULT_NOTICE}
                                onChange={e => setVoice({ ...voice, notice: e.target.value })} />
                        </Field>
                        <Field
                            label="Hand off to a person (optional)"
                            hint="A phone number with its country code, such as +14155550100. The agent can transfer a caller here; if nobody answers, the caller can leave a voicemail."
                            error={transferBad ? 'Write it with its country code, such as +14155550100.' : undefined}
                        >
                            <Input id="phone-transfer" value={voice.transfer_number} invalid={transferBad} className="tabular-nums"
                                onChange={e => setVoice({ ...voice, transfer_number: e.target.value })} placeholder="+14155550100" />
                        </Field>
                        <Field composite label="Recording" hint="Recordings stay in your Twilio account. Transcripts are always kept.">
                            <Segmented
                                id="phone-add-recording"
                                label="Recording"
                                value={voice.recording ? 'on' : 'off'}
                                onChange={v => setVoice({ ...voice, recording: v === 'on' })}
                                options={[{ value: 'off', label: 'Off' }, { value: 'on', label: 'On' }]}
                            />
                        </Field>
                        <Field composite label="Language" hint="The language callers speak. Pick your callers' English (English (India) for Indian callers) and the agent hears them better.">
                            <Select
                                value={toChoice(voice.language)}
                                onChange={v => setVoice({ ...voice, language: fromChoice(v), voice: '', tts_provider: '' })}
                                options={languageOptions(options)}
                                aria-label="Language"
                            />
                        </Field>
                        <p className="m-0 text-xs text-text-faint">{host.costLine}</p>
                    </>
                )}

                <ErrorNote>{error}</ErrorNote>
            </div>
        </Modal>
    );
}
