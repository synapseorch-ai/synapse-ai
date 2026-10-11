'use client';

/*
 * Add a number (V30): one modal, three steps. Your Twilio account (a key,
 * checked straight away by listing its numbers), the number (where each points
 * now), and who answers. Then the number opens.
 *
 * The key is a Restricted or Standard API key, never the account's auth token
 * (V4). A Restricted key connects unsigned and the number says so; a key that
 * cannot change numbers connects with the address to paste into Twilio (V7).
 */
import { ArrowLeft, ExternalLink } from 'lucide-react';
import { useState } from 'react';

import {
    Button, Combobox, ErrorNote, Field, Input, Modal, Segmented, Select, Textarea,
} from '@/components/ui';
import { cn } from '@/lib/cn';

import { usePhoneHost } from './host';
import {
    DEFAULT_NOTICE, E164, RESTRICTED_PERMISSIONS, TWILIO_KEYS_URL, settingsToSend,
    type PhoneAgent, type PhoneNumber, type PhoneOptions, type TwilioNumber, type VoiceSettings,
} from './phone';

const STEPS = ['Your Twilio account', 'The number', 'Who answers'] as const;

const BLANK_VOICE: VoiceSettings = {
    notice: '', filler: '', filler_late: '', transfer_number: '', recording: false, language: '', voice: '',
    tts_provider: '', max_minutes: null,
};

/** Where a number sends its calls now, in words. */
function pointsTo(n: TwilioNumber): string {
    if (n.voice_url) return `Calls go to ${n.voice_url}. Connecting sends them to your agent instead; disconnecting puts this back.`;
    if (n.voice_application_sid) return `Calls go to a TwiML App (${n.voice_application_sid}). Connecting sends them to your agent instead.`;
    return 'Points nowhere yet.';
}

export function languageOptions(options: PhoneOptions) {
    return [
        { value: '', label: "The number's own", hint: `English for its country code, else ${options.default_language}` },
        ...options.languages.map(l => ({ value: l.code, label: l.name, hint: l.code })),
    ];
}

export function AddNumber({
    open, onClose, agents, options, onConnected,
}: {
    open: boolean;
    onClose: () => void;
    agents: PhoneAgent[];
    options: PhoneOptions;
    onConnected: (number: PhoneNumber) => void;
}) {
    const host = usePhoneHost();
    const [step, setStep] = useState(0);
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
            setPicked(p => (found.some(n => n.sid === p) ? p : found[0]?.sid ?? ''));
            setStep(1);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Twilio did not accept the key.');
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
            setError(e instanceof Error ? e.message : 'The number could not be connected.');
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
            description={<span data-phone-step={step + 1}>Step {step + 1} of 3 · {STEPS[step]}</span>}
            footer={footer}
        >
            <div className="grid gap-4">
                {step === 0 && (
                    <>
                        <p className="m-0 text-sm text-text-muted">
                            Make an API key in Twilio for this. We never ask for your account&apos;s auth token.
                        </p>
                        <div className="grid gap-2 rounded-md border border-border bg-surface-2 px-3 py-2.5 text-[13px] text-text-muted">
                            <p className="m-0">
                                In Twilio&apos;s console, under API keys, create a <b className="font-medium text-text">Restricted</b> key
                                with these permissions (the recordings one only if you will record calls):
                            </p>
                            <ul className="m-0 grid gap-0.5 pl-4 font-code text-xs text-text">
                                {RESTRICTED_PERMISSIONS.map(p => <li key={p}>{p}</li>)}
                            </ul>
                            <p className="m-0">
                                A <b className="font-medium text-text">Standard</b> key works too, and also signs every
                                request Twilio sends us. A Restricted key can&apos;t, so its calls are checked by the
                                secret tokens in their addresses only.
                            </p>
                            <a href={TWILIO_KEYS_URL} target="_blank" rel="noreferrer" className="inline-flex w-fit items-center gap-1 text-accent-ink underline-offset-2 hover:underline">
                                Open API keys in Twilio <ExternalLink className="size-3.5" aria-hidden />
                            </a>
                        </div>
                        <Field label="Account SID">
                            <Input id="phone-account-sid" value={accountSid} onChange={e => setAccountSid(e.target.value)}
                                placeholder="AC…" className="font-code" autoComplete="off" spellCheck={false} />
                        </Field>
                        <Field label="API key SID">
                            <Input id="phone-key-sid" value={keySid} onChange={e => setKeySid(e.target.value)}
                                placeholder="SK…" className="font-code" autoComplete="off" spellCheck={false} />
                        </Field>
                        <Field label="API key secret" hint="Checked with Twilio straight away. Stored encrypted and never shown again.">
                            <Input id="phone-key-secret" type="password" value={keySecret} onChange={e => setKeySecret(e.target.value)}
                                className="font-code" autoComplete="new-password" />
                        </Field>
                    </>
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
                                                'flex cursor-pointer items-start gap-3 rounded-md border px-3 py-2.5',
                                                picked === n.sid ? 'border-accent bg-accent-subtle' : 'border-border hover:bg-row-hover',
                                            )}
                                        >
                                            <input type="radio" name="phone-number" value={n.sid} checked={picked === n.sid}
                                                onChange={() => setPicked(n.sid)} className="mt-1 accent-[var(--color-accent)]" />
                                            <span className="min-w-0">
                                                <span className="block text-sm font-medium tabular-nums text-text">{n.number}</span>
                                                <span className="block break-words text-xs text-text-faint">{pointsTo(n)}</span>
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
                            hint="Said on every call before the agent speaks. Leave empty for this default; {agent} and {org} are filled in. With recording on, “This call may be recorded.” is added."
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
                                value={voice.language}
                                onChange={v => setVoice({ ...voice, language: v, voice: '', tts_provider: '' })}
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
