'use client';

/*
 * A call's log (V31), over the number's pane: no page and no route of its own,
 * only `?call=` so the bell can open it.
 *
 * The transcript is the call's own conversation (`voice_{number}_{CallSid}`,
 * V27): what the caller said as Twilio heard it, and what the agent answered as
 * the caller heard it. A barge-in cut the answer there (V14), and the filler
 * the call said while a tool ran is kept beside it (VOX1c). Turns saved after
 * the call ended are the approval's answer, said as such. Colour only for
 * meaning: the machine's tools in Cerulean, a person needed in amber.
 *
 * A question the call parked is answered here too (Naveen at review), as from
 * the bell: the turn carries on behind, and the log reads the conversation
 * again until its result arrives.
 */
import { ArrowLeft, PhoneIncoming, Wrench } from 'lucide-react';
import { useEffect, useState } from 'react';

import { RunChip } from '@/components/assistant';
import { TimeOfDay, When, money, useClock, type Clock } from '@/components/run';
import { Badge, Button, ErrorNote, StatStrip, type StatCell } from '@/components/ui';
import { cn } from '@/lib/cn';

import { usePhoneHost } from './host';
import {
    errorWords,
    callerLabel, clockLength, credits, firstWords, isLive, outcomeOf, outcomeSentence, startedMinutes,
    twilioEstimate, type CallRecord, type CallTurn, type Tone, type Transcript,
} from './phone';

const WORD: Record<Tone, string> = {
    machine: 'text-accent-ink',
    person: 'text-person-ink',
    success: 'text-text-muted',
    danger: 'text-danger',
    neutral: 'text-text-muted',
};
const DOT: Record<Tone, string> = {
    machine: 'bg-machine motion-safe:animate-pulse',
    person: 'bg-person',
    success: 'bg-success',
    danger: 'bg-danger',
    neutral: 'bg-text-faint',
};

/** How a call ended: a dot and its word, in its colour. */
export function OutcomeWord({ call, sentence = false, className }: { call: CallRecord; sentence?: boolean; className?: string }) {
    const { word, tone } = outcomeOf(call);
    return (
        <span className={cn('inline-flex items-center gap-1.5', WORD[tone], className)}>
            <span aria-hidden className={cn('size-1.5 shrink-0 rounded-full', DOT[tone])} />
            {sentence ? outcomeSentence(call) : word}
        </span>
    );
}

/** Minutes and seconds into the call. */
function offset(at: string | null, startedAt: string | null): string | null {
    if (!at || !startedAt) return null;
    const ms = Date.parse(at) - Date.parse(startedAt);
    return Number.isNaN(ms) || ms < 0 ? null : clockLength(ms / 1000);
}

function ToolDetail({ tool }: { tool: CallTurn['tools'][number] }) {
    const args = tool.args && Object.keys(tool.args).length ? JSON.stringify(tool.args) : null;
    return (
        <div className="grid gap-1 rounded-md border border-border bg-surface-2 px-2.5 py-2 font-code text-xs text-text-muted">
            {args && <span className="break-words">{args}</span>}
            {tool.refused
                ? <span className="text-text">Refused{tool.reason ? `: ${tool.reason}` : ''}</span>
                : tool.result && <span className="line-clamp-6 whitespace-pre-wrap break-words text-text">{tool.result}</span>}
        </div>
    );
}

function Turn({ turn, startedAt, after, clock }: { turn: CallTurn; startedAt: string | null; after: boolean; clock: Clock }) {
    // During the call, minutes into it; an approval's answer came later, so it says when.
    const at = after ? null : offset(turn.at, startedAt);
    return (
        <li className="grid gap-3" data-call-turn>
            <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3">
                <span className="text-xs font-medium text-text-muted">Caller</span>
                <p className="m-0 text-sm text-text">{turn.said}</p>
            </div>
            <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3">
                <span className="text-xs font-medium text-accent-ink">
                    Agent
                    {at && <span className="ml-1.5 font-normal tabular-nums text-text-faint">{at}</span>}
                    {after && turn.at && <TimeOfDay iso={turn.at} clock={clock} className="block font-normal text-text-faint" />}
                </span>
                <div className="grid min-w-0 gap-2">
                    {turn.filler.map((line, i) => (
                        <p key={`f${i}`} data-call-filler className="m-0 text-sm italic text-text-faint">{line}</p>
                    ))}
                    {turn.tools.map((tool, i) => (
                        <RunChip
                            key={`t${i}`}
                            icon={<Wrench className="size-3.5" />}
                            label={<span className="font-code">{tool.name}</span>}
                            detail={tool.args || tool.result || tool.refused ? <ToolDetail tool={tool} /> : undefined}
                            tone={tool.refused ? 'danger' : 'default'}
                            className="w-fit max-w-full"
                        />
                    ))}
                    {turn.answer && (
                        <p className="m-0 whitespace-pre-wrap text-sm text-text">
                            {turn.answer}
                            {turn.interrupted && (
                                <span data-call-cut className="ml-1.5 text-xs text-text-faint">(the caller spoke over this)</span>
                            )}
                        </p>
                    )}
                </div>
            </div>
        </li>
    );
}

export function CallLog({
    call, numberName, agentName, onBack,
}: {
    call: CallRecord;
    numberName: string;
    agentName: string;
    onBack: () => void;
}) {
    const host = usePhoneHost();
    const clock = useClock();
    const [transcript, setTranscript] = useState<Transcript | null>(null);
    const [error, setError] = useState('');
    const [cost, setCost] = useState<number | null | undefined>(undefined);
    const [answering, setAnswering] = useState<'allow' | 'refuse' | null>(null);
    const [answered, setAnswered] = useState<'allowed' | 'refused' | null>(null);
    const [answerError, setAnswerError] = useState('');
    const [laterBefore, setLaterBefore] = useState(0);
    const [reread, setReread] = useState(0);

    useEffect(() => {
        let gone = false;
        host.transcript(call)
            .then(t => { if (!gone) { setTranscript(t); setError(''); } })
            .catch(e => { if (!gone) setError(errorWords(e, 'The conversation could not be read.')); });
        host.modelCost(call).then(c => { if (!gone) setCost(c); });
        return () => { gone = true; };
    }, [host, call, reread]);

    // Turns saved after the call ended are an approval's answer; the rest is the call.
    const ended = call.endedAt ? Date.parse(call.endedAt) : NaN;
    const turns = transcript?.turns ?? [];
    const during = turns.filter(t => Number.isNaN(ended) || !t.at || Date.parse(t.at) <= ended + 1000);
    const later = turns.filter(t => !during.includes(t));

    // After an answer the turn carries on behind: read the conversation again until its result is saved (30 s at most).
    const settled = answered !== null && later.length > laterBefore;
    useEffect(() => {
        if (answered === null || settled || reread > 15) return;
        const t = window.setTimeout(() => setReread(r => r + 1), 2000);
        return () => window.clearTimeout(t);
    }, [answered, settled, reread]);

    const answer = async (approve: boolean) => {
        if (!transcript?.pending) return;
        setAnswering(approve ? 'allow' : 'refuse'); setAnswerError('');
        try {
            await host.answer(call, approve, transcript.pending.approvalId);
            setLaterBefore(later.length);
            setAnswered(approve ? 'allowed' : 'refused');
            setReread(r => r + 1);
        } catch (e) {
            setAnswerError(errorWords(e, 'That answer did not go through.'));
        } finally {
            setAnswering(null);
        }
    };

    const live = isLive(call);
    const minutes = startedMinutes(call.seconds);
    const fw = firstWords(call);
    const charged = credits(call);
    const cells: StatCell[] = [
        { label: 'Length', value: clockLength(call.seconds), sub: fw === null ? undefined : `First words in ${fw} s` },
        ...(host.credits ? [{
            label: 'This call',
            value: charged === null ? '—' : `${charged.toLocaleString()} credits`,
            sub: charged === null ? (live ? 'Charged when it ends' : 'Not settled yet') : `${minutes} started min`,
        }] : []),
        { label: 'Model', value: cost === undefined ? '…' : money(cost), sub: cost === null ? 'Nothing recorded' : undefined },
        { label: 'Twilio, estimated', value: money(twilioEstimate(call)), sub: 'At US rates' },
    ];
    return (
        <div className="grid gap-6" data-call-log={call.id}>
            <Button variant="ghost" size="sm" className="w-fit" onClick={onBack} aria-label={`Back to ${numberName}`}>
                <ArrowLeft className="size-4" aria-hidden /> {numberName}
            </Button>
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex min-w-0 items-start gap-3">
                    <PhoneIncoming className="mt-1 size-5 shrink-0 text-text-faint" aria-hidden />
                    <div className="min-w-0">
                        <h2 className="m-0 truncate text-lg font-semibold tracking-[-0.01em] text-text tabular-nums">
                            {call.caller.startsWith('client:') ? callerLabel(call.caller) : `Call from ${callerLabel(call.caller)}`}
                        </h2>
                        <p className="m-0 mt-0.5 flex flex-wrap items-center gap-x-2 text-[13px] text-text-muted">
                            <When iso={call.startedAt} clock={clock} />
                            <span aria-hidden>·</span>
                            <span>{agentName}</span>
                            <span aria-hidden>·</span>
                            <OutcomeWord call={call} sentence />
                        </p>
                    </div>
                </div>
            </div>

            <StatStrip size="sm" label="This call" cells={cells} />

            {call.error && <ErrorNote>{call.error}</ErrorNote>}

            {transcript?.pending && (
                <div data-call-pending className="grid gap-1 rounded-md border border-person/40 bg-person-subtle px-3 py-2.5 text-sm">
                    <span className="inline-flex items-center gap-2 font-medium text-person-ink">
                        <Badge tone="person">Waiting on you</Badge> Asked for approval · {transcript.pending.tool}
                    </span>
                    <span className="break-words font-code text-xs text-text-muted">{JSON.stringify(transcript.pending.args)}</span>
                    <span className="text-xs text-text-muted">
                        The caller was told someone will follow up. Approve or refuse it here or from the bell, then call them back.
                    </span>
                    {host.canEdit && (
                        <div className="mt-1.5 flex flex-wrap items-center gap-2">
                            <Button size="sm" variant="person" onClick={() => void answer(true)} loading={answering === 'allow'} disabled={answering !== null}>
                                Approve
                            </Button>
                            <Button size="sm" variant="secondary" onClick={() => void answer(false)} loading={answering === 'refuse'} disabled={answering !== null}>
                                Refuse
                            </Button>
                        </div>
                    )}
                    {answerError && <p role="alert" className="m-0 text-xs text-danger">{answerError}</p>}
                </div>
            )}
            {answered && !transcript?.pending && (
                <p role="status" data-call-answered className="m-0 text-sm text-text-muted">
                    {answered === 'allowed'
                        ? settled ? 'Allowed. It ran, and its result is below. Call them back.' : 'Allowed. It runs now; its result appears below when it is done.'
                        : 'Refused, not run. Let the caller know.'}
                </p>
            )}
            {!transcript?.pending && !answered && call.approvals.length > 0 && (
                <p className="m-0 inline-flex items-center gap-2 text-sm text-person-ink">
                    <span aria-hidden className="size-1.5 rounded-full bg-person" />
                    This call asked for approval; the caller was told someone will follow up.
                    {later.length ? ' Its answer is below.' : ''}
                </p>
            )}

            <ErrorNote>{error}</ErrorNote>
            {!transcript && !error && <p className="m-0 text-sm text-text-faint">Reading the conversation…</p>}
            {transcript && turns.length === 0 && (
                <p className="m-0 text-sm text-text-faint">
                    {live ? 'Nothing said yet.' : 'The caller said nothing the agent answered.'}
                </p>
            )}
            {during.length > 0 && (
                <ol aria-label="Transcript" className="m-0 grid list-none gap-5 p-0">
                    {during.map((t, i) => <Turn key={i} turn={t} startedAt={call.startedAt} after={false} clock={clock} />)}
                </ol>
            )}

            {call.transfer && (
                <p className="m-0 text-sm text-text-muted">
                    Transferred to a person{call.transfer.reason ? `: ${call.transfer.reason}` : ''}
                    {call.transfer.status ? ` (${call.transfer.status.replace(/-/g, ' ')})` : ''}.
                </p>
            )}
            {call.voicemail && (
                <p data-call-voicemail className="m-0 inline-flex flex-wrap items-center gap-2 text-sm text-person-ink">
                    <span aria-hidden className="size-1.5 rounded-full bg-person" />
                    The caller left a voicemail
                    {call.voicemail.seconds ? ` (${clockLength(call.voicemail.seconds)})` : ''}. It is in your Twilio account
                    {call.voicemail.recording_sid ? <span className="font-code text-xs text-text-muted">{call.voicemail.recording_sid}</span> : null}
                </p>
            )}

            {later.length > 0 && (
                <div className="grid gap-4 border-t border-border pt-5">
                    <h3 className="m-0 text-sm font-medium text-text">After the call: the approval&apos;s answer</h3>
                    <ol aria-label="After the call" className="m-0 grid list-none gap-5 p-0">
                        {later.map((t, i) => <Turn key={i} turn={t} startedAt={call.startedAt} after clock={clock} />)}
                    </ol>
                </div>
            )}

            {!live && (
                <p className="m-0 border-t border-border pt-3 text-xs text-text-faint tabular-nums">
                    {outcomeSentence(call)} at {clockLength(call.seconds)}.
                    {' '}Recording was {call.recordingSid ? <>on (<span className="font-code">{call.recordingSid}</span>, in your Twilio account)</> : 'off'}.
                </p>
            )}
        </div>
    );
}
