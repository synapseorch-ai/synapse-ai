'use client';

/*
 * A number's pane (V31): the number and whether it is taking calls, its
 * recent calls first, who answers and how (the agent, the notice, the filler,
 * the hand-off number, recording, language and voice), and what a caller may
 * make the agent do (D233's picker, none ticked).
 *
 * One Save covers the form: the APIs replace a number's settings whole, so a
 * save sends every one of them. Turning the number off or on, and deleting it,
 * act at once and say what Twilio kept (`left`), never hidden.
 */
import { Copy, Info, Phone, Power, RefreshCw, Trash } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { DetailHead, When, useClock, type Clock } from '@/components/run';
import {
    Button, Combobox, ConfirmDialog, ErrorNote, Field, IconButton, Input, Section, Segmented, Select, Textarea,
    Toast, useToast,
} from '@/components/ui';
import { cn } from '@/lib/cn';

import { languageOptions } from './AddNumber';
import { CallLog, OutcomeWord } from './CallLog';
import { usePhoneHost, type NumberPatch } from './host';
import {
    errorWords,
    DEFAULT_FILLER, DEFAULT_FILLER_LATE, DEFAULT_NOTICE, NOTICE_HINT, E164, agentNotes, callSeconds, callerLabel, clockLength,
    UNSET, fromChoice, isLive, settingsOf, settingsToSend, toChoice, type CallRecord, type PhoneAgent, type PhoneNumber, type PhoneOptions,
    type VoiceSettings,
} from './phone';

type Draft = { name: string; agentId: string; visitorTools: string[]; voice: VoiceSettings };

function draftOf(n: PhoneNumber): Draft {
    return { name: n.name, agentId: n.agentIds[0] ?? '', visitorTools: [...n.visitorTools], voice: settingsOf(n.voice) };
}

const VOICE_EXAMPLE: Record<string, string> = {
    ElevenLabs: 'A voice ID, such as UgBBYS2sOqTuMpoF3BR0',
    Google: 'A voice name, such as en-US-Neural2-F',
    Amazon: 'A voice name, such as Joanna-Neural',
};

/** Whether the number takes calls, in words: quiet when it does, red when it can't. */
function Status({ n }: { n: PhoneNumber }) {
    const broken = n.status === 'error' || (!!n.lastError && n.enabled);
    const [word, dot, text] = !n.enabled
        ? ['Off', 'bg-text-faint', 'text-text-muted']
        : broken
            ? ["Can't take calls", 'bg-danger', 'text-danger']
            : n.status === 'running'
                ? ['Receiving calls', 'bg-success', 'text-text-muted']
                : ['Not receiving calls', 'bg-text-faint', 'text-text-muted'];
    return (
        <span className={cn('inline-flex items-center gap-1.5', text)} data-phone-status>
            <span aria-hidden className={cn('size-1.5 rounded-full', dot)} />{word}
        </span>
    );
}

function Note({ children, tone = 'neutral' }: { children: React.ReactNode; tone?: 'neutral' | 'person' }) {
    return (
        <div className={cn(
            'flex items-start gap-2 rounded-md border px-3 py-2.5 text-[13px] leading-relaxed',
            tone === 'person' ? 'border-person/40 bg-person-subtle text-text' : 'border-border bg-surface-2 text-text-muted',
        )}>
            <Info className={cn('mt-0.5 size-4 shrink-0', tone === 'person' ? 'text-person-ink' : 'text-text-faint')} aria-hidden />
            <div className="min-w-0">{children}</div>
        </div>
    );
}

function CallRow({ call, onOpen, now, clock }: { call: CallRecord; onOpen: () => void; now: number; clock: Clock }) {
    return (
        <li className="m-0">
            <button
                type="button"
                onClick={onOpen}
                data-call-row={call.id}
                className="grid w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-0.5 border-b border-border px-2.5 py-2.5 text-left transition-colors hover:bg-row-hover sm:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_3.5rem_minmax(0,1.4fr)]"
            >
                <span className="truncate text-sm font-medium tabular-nums text-text">{callerLabel(call.caller)}</span>
                <When iso={call.startedAt} clock={clock} className="text-xs text-text-faint max-sm:hidden" />
                <span className="text-right text-xs tabular-nums text-text-muted max-sm:hidden">{clockLength(callSeconds(call, now))}</span>
                <OutcomeWord call={call} className="justify-self-end text-xs sm:justify-self-start" />
                <span className="col-span-2 text-xs text-text-faint sm:hidden">
                    <When iso={call.startedAt} clock={clock} /> · {clockLength(callSeconds(call, now))}
                </span>
            </button>
        </li>
    );
}

export function NumberPane({
    number, agents, options, openCall, onOpenCall, onChanged, onRemoved,
}: {
    number: PhoneNumber;
    agents: PhoneAgent[];
    options: PhoneOptions;
    /** The CallSid whose log is open over the pane, if any. */
    openCall: string | null;
    onOpenCall: (callSid: string | null) => void;
    onChanged: (n: PhoneNumber) => void;
    onRemoved: (left: string[]) => void;
}) {
    const host = usePhoneHost();
    const { toast, show, dismiss } = useToast();
    const [calls, setCalls] = useState<CallRecord[] | null>(null);
    const [callsError, setCallsError] = useState('');
    const [reread, setReread] = useState(0);
    const [draft, setDraft] = useState<Draft>(() => draftOf(number));
    const [saving, setSaving] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [left, setLeft] = useState<string[]>([]);
    const [confirmDelete, setConfirmDelete] = useState(false);
    const [now, setNow] = useState(() => Date.now());
    const clock = useClock();

    useEffect(() => {
        let gone = false;
        host.calls(number.id)
            .then(list => { if (!gone) { setCalls(list); setCallsError(''); setNow(Date.now()); } })
            .catch(e => { if (!gone) setCallsError(errorWords(e, 'The calls could not be read.')); });
        return () => { gone = true; };
    }, [host, number.id, reread]);

    // While a call is on the line, read the list again so it ends on its own.
    const anyLive = !!calls?.some(isLive);
    useEffect(() => {
        if (!anyLive) return;
        const t = window.setInterval(() => setReread(r => r + 1), 10_000);
        return () => window.clearInterval(t);
    }, [anyLive]);

    const saved = useMemo(() => draftOf(number), [number]);
    const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
    const agent = agents.find(a => a.id === draft.agentId);
    const notes = agentNotes(agent, options.fast_for_voice);
    const language = options.languages.find(l => l.code === (draft.voice.language || options.default_language));
    const transferBad = !!draft.voice.transfer_number.trim() && !E164.test(draft.voice.transfer_number.trim());
    const v = draft.voice;
    const setVoice = (patch: Partial<VoiceSettings>) => setDraft(d => ({ ...d, voice: { ...d.voice, ...patch } }));
    const readOnly = !host.canEdit;

    const save = async () => {
        setSaving(true); setError('');
        const patch: NumberPatch = {};
        if (draft.name.trim() !== saved.name) patch.name = draft.name.trim() || number.voice.number || saved.name;
        if (draft.agentId && draft.agentId !== saved.agentId) patch.agentId = draft.agentId;
        if (JSON.stringify(draft.visitorTools) !== JSON.stringify(saved.visitorTools)) patch.visitorTools = draft.visitorTools;
        if (JSON.stringify(draft.voice) !== JSON.stringify(saved.voice)) {
            patch.voice = settingsToSend({ ...draft.voice, transfer_number: draft.voice.transfer_number.trim() }, options);
        }
        try {
            const next = await host.save(number, patch);
            onChanged(next);
            setDraft(draftOf(next));
            show('Saved. The next call uses it.');
        } catch (e) {
            setError(errorWords(e, 'The number could not be saved.'));
        } finally {
            setSaving(false);
        }
    };
    const toggle = async () => {
        setBusy(true); setError(''); setLeft([]);
        try {
            const out = await host.setEnabled(number, !number.enabled);
            setLeft(out.left);
            if (out.number) onChanged(out.number);
            show(number.enabled ? 'Turned off. The number is back where it pointed before.' : 'Turned on.');
        } catch (e) {
            setError(errorWords(e, 'That did not work.'));
        } finally {
            setBusy(false);
        }
    };
    const remove = async () => {
        setConfirmDelete(false); setBusy(true); setError('');
        try {
            const out = await host.remove(number);
            onRemoved(out.left);
        } catch (e) {
            setError(errorWords(e, 'The number could not be deleted.'));
            setBusy(false);
        }
    };

    const open = openCall ? calls?.find(c => c.id === openCall) : undefined;
    if (open) {
        return (
            <CallLog
                call={open}
                numberName={number.name}
                agentName={agents.find(a => a.id === open.agentId)?.name ?? 'The agent'}
                onBack={() => onOpenCall(null)}
            />
        );
    }
    const missing = openCall && calls !== null;

    return (
        <div className="grid gap-6" data-phone-pane={number.id}>
            <DetailHead
                leading={<Phone className="mt-1 size-5 shrink-0 text-text-faint" aria-hidden />}
                title={number.name}
                sub={
                    <span className="inline-flex flex-wrap items-center gap-x-2">
                        {/* A number made without one (a trial's browser-call app) has nothing to show here. */}
                        {number.voice.number && <><span className="tabular-nums">{number.voice.number}</span><span aria-hidden>·</span></>}
                        Twilio<span aria-hidden>·</span><Status n={number} />
                    </span>
                }
                actions={host.canEdit && (
                    <>
                        <Button variant="secondary" onClick={toggle} loading={busy}>
                            {!busy && <Power className="size-4" aria-hidden />} {number.enabled ? 'Turn off' : 'Turn on'}
                        </Button>
                        <IconButton icon={Trash} tone="danger" label={`Delete ${number.name}`} title="Delete" onClick={() => setConfirmDelete(true)} />
                    </>
                )}
            />

            {number.enabled && number.lastError && <ErrorNote>{number.lastError}</ErrorNote>}
            <ErrorNote>{error}</ErrorNote>
            {left.map(sentence => <Note key={sentence}>{sentence}</Note>)}
            {number.voice.manual_url && (
                <Note tone="person">
                    <p className="m-0 font-medium">Point this number by hand</p>
                    <p className="m-0">
                        {number.voice.point_error ? `${number.voice.point_error} ` : ''}In Twilio&apos;s console, set the
                        number&apos;s Voice URL (A call comes in, webhook, HTTP POST) to:
                    </p>
                    <p className="m-0 mt-1 flex items-center gap-1.5">
                        <span className="break-all font-code text-xs text-text">{number.voice.manual_url}</span>
                        <IconButton
                            size="sm"
                            icon={Copy}
                            label="Copy the address"
                            onClick={() => { void navigator.clipboard?.writeText(String(number.voice.manual_url)); show('Copied.'); }}
                        />
                    </p>
                </Note>
            )}
            {number.voice.signing_error && (
                <Note>
                    <span className="font-medium text-text">Calls are checked, not signed.</span> {number.voice.signing_error}
                </Note>
            )}

            <Section
                title="Recent calls"
                variant="plain"
                collapsible={false}
                actions={<IconButton size="sm" icon={RefreshCw} label="Read the calls again" onClick={() => setReread(r => r + 1)} />}
            >
                {missing && <Note>That call isn&apos;t among this number&apos;s recent calls.</Note>}
                <ErrorNote>{callsError}</ErrorNote>
                {calls === null && !callsError && <p className="m-0 text-sm text-text-faint">Reading the calls…</p>}
                {calls?.length === 0 && (
                    <p className="m-0 border-t border-border px-2.5 py-3 text-sm text-text-faint">
                        No calls yet. Call <span className="tabular-nums">{number.voice.number}</span> to try it.
                    </p>
                )}
                {!!calls?.length && (
                    <ul aria-label="Recent calls" className="m-0 list-none border-t border-border p-0">
                        {calls.map(c => <CallRow key={c.id} call={c} now={now} clock={clock} onOpen={() => onOpenCall(c.id)} />)}
                    </ul>
                )}
            </Section>

            <Section title="Who answers" variant="plain" collapsible={false}>
                <fieldset disabled={readOnly} className="m-0 grid min-w-0 gap-5 border-0 p-0">
                    <Field composite label="Agent">
                        <Combobox
                            value={draft.agentId}
                            onChange={agentId => setDraft(d => ({ ...d, agentId, visitorTools: agentId === saved.agentId ? d.visitorTools : [] }))}
                            options={agents.map(a => ({ value: a.id, label: a.name }))}
                            placeholder="Choose an agent"
                            searchPlaceholder="Search agents…"
                            emptyMessage="No agents yet."
                            disabled={readOnly}
                            aria-label="Agent"
                        />
                    </Field>
                    {notes.length > 0 && (
                        <div className="-mt-2 grid gap-1.5" data-phone-notes>
                            {notes.map(n => (
                                <p key={n} className="m-0 flex items-start gap-2 text-[13px] text-text-muted">
                                    <Info className="mt-0.5 size-3.5 shrink-0 text-text-faint" aria-hidden />
                                    <span>{n}</span>
                                </p>
                            ))}
                            {agent && (
                                <a href={host.links.agent(agent.id)} className="ml-5.5 w-fit text-[13px] text-accent-ink underline-offset-2 hover:underline">
                                    Open {agent.name}&apos;s settings
                                </a>
                            )}
                        </div>
                    )}
                    <Field label="Name" hint="How this number is listed here. Callers never hear it.">
                        <Input id="phone-pane-name" value={draft.name} onChange={e => setDraft(d => ({ ...d, name: e.target.value }))} />
                    </Field>
                    <Field
                        label="What callers hear first"
                        hint={NOTICE_HINT}
                    >
                        <Textarea id="phone-pane-notice" rows={2} value={v.notice} placeholder={DEFAULT_NOTICE}
                            onChange={e => setVoice({ notice: e.target.value })} />
                    </Field>
                    <Field label="While the agent looks something up" hint="Said once a turn, when the agent starts a tool and has said nothing yet.">
                        <Input id="phone-pane-filler" value={v.filler} placeholder={DEFAULT_FILLER} onChange={e => setVoice({ filler: e.target.value })} />
                    </Field>
                    <Field label="If the tool is still running after 6 seconds">
                        <Input id="phone-pane-filler-late" value={v.filler_late} placeholder={DEFAULT_FILLER_LATE} onChange={e => setVoice({ filler_late: e.target.value })} />
                    </Field>
                    <Field
                        label="Hand off to a person"
                        hint="A phone number with its country code. The agent can transfer a caller here; if nobody answers, the caller can leave a voicemail. Leave empty to turn transfers off."
                        error={transferBad ? 'Write it with its country code, such as +14155550100.' : undefined}
                    >
                        <Input id="phone-pane-transfer" value={v.transfer_number} placeholder="+14155550100" className="tabular-nums"
                            onChange={e => setVoice({ transfer_number: e.target.value })} />
                    </Field>
                    <Field composite label="Recording" hint="Recordings stay in your Twilio account. Transcripts are always kept.">
                        <Segmented
                            id="phone-pane-recording"
                            label="Recording"
                            value={v.recording ? 'on' : 'off'}
                            onChange={r => setVoice({ recording: r === 'on' })}
                            options={[{ value: 'off', label: 'Off' }, { value: 'on', label: 'On' }]}
                        />
                    </Field>
                    <div className="grid gap-5 sm:grid-cols-2">
                        <Field composite label="Language" hint="Match your callers: English (India) hears an Indian accent better than English (United States).">
                            <Select
                                value={toChoice(v.language)}
                                onChange={choice => {
                                    const language = fromChoice(choice);
                                    const next = options.languages.find(l => l.code === (language || options.default_language));
                                    const keep = !!next?.tts.includes(v.tts_provider);
                                    setVoice({ language, ...(keep ? {} : { tts_provider: '', voice: '' }) });
                                }}
                                options={languageOptions(options)}
                                aria-label="Language"
                            />
                        </Field>
                        <Field composite label="Voice">
                            <Select
                                value={toChoice(v.tts_provider)}
                                onChange={choice => {
                                    const tts_provider = fromChoice(choice);
                                    setVoice({ tts_provider, voice: tts_provider ? v.voice : '' });
                                }}
                                options={[
                                    { value: UNSET, label: "Twilio's default voice" },
                                    ...(language?.tts ?? options.tts_providers).map(p => ({ value: p, label: p })),
                                ]}
                                aria-label="Voice"
                            />
                        </Field>
                    </div>
                    {v.tts_provider && (
                        <Field label={`${v.tts_provider} voice`} hint="As the provider names it. Twilio's ConversationRelay documentation lists the voices for each language.">
                            <Input id="phone-pane-voice" value={v.voice} placeholder={VOICE_EXAMPLE[v.tts_provider]} className="font-code"
                                onChange={e => setVoice({ voice: e.target.value })} />
                        </Field>
                    )}
                    {options.speech_models && language && (
                        <div className="grid gap-5 sm:grid-cols-2" data-phone-transcription>
                            <Field composite label="Transcription" hint="Who hears the caller.">
                                <Select
                                    value={toChoice(v.transcription_provider)}
                                    onChange={choice => setVoice({ transcription_provider: fromChoice(choice), speech_model: '' })}
                                    options={[
                                        {
                                            value: UNSET,
                                            // Twilio hears the caller unless the number picks someone (Naveen, VOX1d).
                                            label: 'Twilio’s default',
                                            hint: language.default_stt ? `${language.default_stt.provider} ${language.default_stt.model}` : undefined,
                                        },
                                        ...Object.keys(language.stt).map(p => ({ value: p, label: p })),
                                    ]}
                                    aria-label="Transcription"
                                />
                            </Field>
                            {v.transcription_provider && (
                                <Field composite label="Speech model">
                                    <Select
                                        value={toChoice(v.speech_model)}
                                        onChange={choice => setVoice({ speech_model: fromChoice(choice) })}
                                        options={[
                                            { value: UNSET, label: 'The provider’s default' },
                                            ...(language.stt[v.transcription_provider] ?? []).map(m => ({ value: m, label: m })),
                                        ]}
                                        aria-label="Speech model"
                                    />
                                </Field>
                            )}
                        </div>
                    )}
                    <Field label="Longest call, in minutes" hint={`Leave empty for ${host.longestCall}. A minute before the end the agent is told to wrap up.`}>
                        <Input
                            id="phone-pane-max"
                            type="number"
                            min={1}
                            max={240}
                            value={v.max_minutes ?? ''}
                            onChange={e => setVoice({ max_minutes: e.target.value ? Math.max(1, Math.min(240, Number(e.target.value))) : null })}
                            className="w-32 tabular-nums"
                        />
                    </Field>
                </fieldset>
            </Section>

            <Section title="Who calls this number" variant="plain" collapsible={false}>
                <div className="grid gap-3">
                    <p className="m-0 text-[13px] text-text-muted">
                        Callers are always visitors: a phone number can be faked, so it never makes someone a member of
                        this workspace. Choose what the agent may do for them. A tool that needs approval asks someone
                        here, and the caller is told someone will follow up.
                    </p>
                    {draft.agentId ? (
                        <host.VisitorTools
                            agentIds={[draft.agentId]}
                            ticked={draft.visitorTools}
                            onChange={visitorTools => setDraft(d => ({ ...d, visitorTools }))}
                            disabled={readOnly}
                        />
                    ) : (
                        <p className="m-0 text-[13px] text-text-faint">Choose the agent first: its tools are the ones a caller may use.</p>
                    )}
                </div>
            </Section>

            {host.canEdit && dirty && (
                <div className="sticky bottom-0 z-10 -mx-1 flex flex-wrap items-center justify-end gap-2 border-t border-border bg-bg/95 px-1 py-3 backdrop-blur" data-phone-savebar>
                    <span className="mr-auto text-[13px] text-text-muted">Unsaved changes</span>
                    <Button variant="ghost" onClick={() => { setDraft(saved); setError(''); }} disabled={saving}>Discard</Button>
                    <Button onClick={save} loading={saving} disabled={transferBad || !draft.agentId}>Save</Button>
                </div>
            )}

            <ConfirmDialog
                open={confirmDelete}
                onClose={() => setConfirmDelete(false)}
                onConfirm={remove}
                title={`Delete ${number.name}?`}
                description="The number goes back to where it pointed before, and calls stop reaching your agent. The number itself stays yours in Twilio."
                confirmLabel="Delete"
            />
            <Toast message={toast.message} tone={toast.tone} onDismiss={dismiss} />
        </div>
    );
}
