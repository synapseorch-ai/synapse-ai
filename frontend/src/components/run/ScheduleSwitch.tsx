'use client';

/*
 * A schedule turned on or paused from its row (F1452): the On / Paused switch in the list's status column, the row
 * menu's Turn on / Pause, and the ask before it is turned on (`scheduleSwitchState.ts` says why it asks and what it
 * says). Both products draw it; each brings its own save — OSS's PATCH, cloud's — and its own toast. Nothing here
 * reads an API.
 */
import { useCallback, useState, type ReactNode } from 'react';

import { Button, Modal } from '@/components/ui';

import { when } from './model';
import {
    refusedWords, settled, shownEnabled, turnOnWords, withAsk, withoutAsk, type EnabledAsks,
} from './scheduleSwitchState';
import { type ScheduleLike } from './workflowRows';

export type SwitchSchedule = ScheduleLike & { id: string; name: string; enabled: boolean; prompt?: string | null };

export function useScheduleSwitch<S extends SwitchSchedule>({
    schedules, save, say, turnOnNote,
}: {
    schedules: S[];
    /** The product's save of `enabled`; its answer's `next_run_at` is the toast's next run. Throws when refused. */
    save: (schedule: S, on: boolean) => Promise<{ next_run_at?: string | null } | null | void>;
    say: (message: string, tone: 'success' | 'danger') => void;
    /** One more line in the ask: cloud's, that it will run as the person who turns it on (F1452, Naveen's (b)). */
    turnOnNote?: ReactNode;
}): {
    /** What the row shows while its save is out. */
    enabledOf: (id: string, enabled: boolean) => boolean;
    /** Whether the row's save is out. */
    busy: (id: string) => boolean;
    /** Pause at once; turn on after the ask. */
    ask: (id: string, on: boolean) => void;
    /** The ask before turning on: draw it once, anywhere on the page. */
    confirm: ReactNode;
} {
    const [asks, setAsks] = useState<EnabledAsks>({});
    const [out, setOut] = useState<ReadonlySet<string>>(new Set());
    // The schedule being asked about, and when the ask opened: its first run counts from then.
    const [asking, setAsking] = useState<{ schedule: S; at: number } | null>(null);

    // An ask is dropped once the list says the same: OSS's at once, cloud's when its page has read again. Done as
    // the list arrives (React's "adjusting state when a prop changes"), so no render shows a settled ask.
    const [seen, setSeen] = useState(schedules);
    if (seen !== schedules) {
        setSeen(schedules);
        setAsks((a) => settled(a, schedules));
    }

    const send = useCallback(async (s: S, on: boolean) => {
        setAsks((a) => withAsk(a, s.id, on));
        setOut((o) => new Set(o).add(s.id));
        try {
            const answer = await save(s, on);
            const next = answer && on ? answer.next_run_at : null;
            say(on
                ? `${s.name} is on.${next ? ` Next run ${when(next, { now: Date.now() })}.` : ''}`
                : `Paused ${s.name}.`, 'success');
        } catch (err) {
            setAsks((a) => withoutAsk(a, s.id));
            say(refusedWords(err, on, s.name), 'danger');
        } finally {
            setOut((o) => { const n = new Set(o); n.delete(s.id); return n; });
        }
    }, [save, say]);

    const ask = useCallback((id: string, on: boolean) => {
        const s = schedules.find((x) => x.id === id);
        if (!s) return;
        if (on) setAsking({ schedule: s, at: Date.now() });
        else void send(s, false);
    }, [schedules, send]);

    const enabledOf = useCallback((id: string, enabled: boolean) => shownEnabled(asks, id, enabled), [asks]);
    const busy = useCallback((id: string) => out.has(id), [out]);

    const confirm = (
        <TurnOnAsk
            schedule={asking?.schedule ?? null}
            at={asking?.at ?? 0}
            note={turnOnNote}
            onClose={() => setAsking(null)}
            onTurnOn={() => {
                const s = asking?.schedule;
                setAsking(null);
                if (s) void send(s, true);
            }}
        />
    );
    return { enabledOf, busy, ask, confirm };
}

function TurnOnAsk({ schedule, at, note, onClose, onTurnOn }: {
    schedule: SwitchSchedule | null; at: number; note?: ReactNode; onClose: () => void; onTurnOn: () => void;
}) {
    const said = schedule ? turnOnWords(schedule, at) : null;
    return (
        <Modal
            open={schedule !== null}
            onClose={onClose}
            title={`Turn on ${schedule?.name ?? ''}?`}
            description="It runs on its own from now on, until it is paused."
            size="sm"
            footer={
                <>
                    {/* Cancel first and focused: Enter on the switch that opened this must not turn it on. */}
                    <Button variant="secondary" onClick={onClose} autoFocus>Cancel</Button>
                    <Button onClick={onTurnOn}>Turn on</Button>
                </>
            }
        >
            {said && (
                <div className="grid gap-4 text-sm">
                    <div className="grid gap-1.5">
                        <p className="m-0 font-medium text-text">Each run starts with this message</p>
                        {said.message ? (
                            <p
                                data-turn-on-message
                                className="m-0 max-h-40 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-surface-2 px-3 py-2 text-text"
                            >
                                {said.message}
                            </p>
                        ) : (
                            <p className="m-0 text-text-muted">No message: each run starts with nothing to read.</p>
                        )}
                    </div>
                    <p data-turn-on-next className="m-0 text-text-muted">
                        {said.next.kind === 'at' ? (
                            <>First run <span className="font-medium text-text">{when(said.next.iso, { now: at })}</span>.</>
                        ) : (
                            <>Runs <span className="font-medium text-text">{said.next.words}</span>; the first run is the next of those.</>
                        )}
                    </p>
                    {note && <p data-turn-on-note className="m-0 text-text-muted">{note}</p>}
                </div>
            )}
        </Modal>
    );
}
