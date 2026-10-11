'use client';

/*
 * The Phone tab (V29, Naveen: its own tab in the Channels hub): the numbers
 * beside the open number, never an empty pane (M2h-2). A number is a
 * `messaging_channels` document like any channel, but the Channels tab lists
 * none of them.
 *
 * `number` and `call` come from the page's address (`?number=&call=`), so the
 * bell can open a call's log (VOX1c). The screen writes them back as it moves,
 * with `history.replaceState`: no navigation, and Next's router follows a
 * replace whose state is null (SW1). A link that changes them remounts the
 * screen (each product keys it by them).
 */
import { Info, Phone, Plus, RefreshCw } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { LIST_DETAIL, ListRow, showDetail } from '@/components/run';
import { Button, EmptyState, ErrorNote, IconButton } from '@/components/ui';
import { cn } from '@/lib/cn';

import { AddNumber } from './AddNumber';
import { usePhoneHost } from './host';
import { NumberPane } from './NumberPane';
import { EMPTY_OPTIONS, type PhoneAgent, type PhoneNumber, type PhoneOptions } from './phone';

export type PhoneInitial = { numbers: PhoneNumber[]; options: PhoneOptions; agents: PhoneAgent[] };

function writeQuery(number: string | null, call: string | null) {
    if (typeof window === 'undefined') return;
    const url = new URL(window.location.href);
    if (number) url.searchParams.set('number', number); else url.searchParams.delete('number');
    if (call) url.searchParams.set('call', call); else url.searchParams.delete('call');
    if (url.href !== window.location.href) window.history.replaceState(null, '', url.pathname + url.search + url.hash);
}

function rowStatus(n: PhoneNumber): { word: string; tone: string } {
    if (!n.enabled) return { word: 'Off', tone: 'text-text-faint' };
    if (n.status === 'error' || n.lastError) return { word: "Can't take calls", tone: 'text-danger' };
    if (n.voice.manual_url) return { word: 'Point it in Twilio', tone: 'text-person-ink' };
    return { word: n.status === 'running' ? 'On' : '', tone: 'text-text-faint' };
}

export function PhoneScreen({
    number: wantNumber, call: wantCall, initial,
}: {
    number?: string;
    call?: string;
    /** What the page read on the server (cloud); without it the screen reads through the host. */
    initial?: PhoneInitial;
}) {
    const host = usePhoneHost();
    const detailRef = useRef<HTMLElement>(null);
    const [numbers, setNumbers] = useState<PhoneNumber[] | null>(initial?.numbers ?? null);
    const [options, setOptions] = useState<PhoneOptions>(initial?.options ?? EMPTY_OPTIONS);
    const [agents, setAgents] = useState<PhoneAgent[]>(initial?.agents ?? []);
    const [selected, setSelected] = useState<string | null>(wantNumber ?? null);
    const [openCall, setOpenCall] = useState<string | null>(wantCall ?? null);
    const [adding, setAdding] = useState(false);
    const [left, setLeft] = useState<string[]>([]);
    const [loadError, setLoadError] = useState('');
    const [reread, setReread] = useState(initial ? 0 : 1);

    useEffect(() => {
        if (!reread) return;
        let gone = false;
        Promise.all([host.numbers(), host.options(), host.agents()])
            .then(([n, o, a]) => { if (!gone) { setNumbers(n); setOptions(o); setAgents(a); setLoadError(''); } })
            .catch(e => { if (!gone) { setNumbers(prev => prev ?? []); setLoadError(e instanceof Error ? e.message : 'The numbers could not be read.'); } });
        return () => { gone = true; };
    }, [host, reread]);

    // UX1: never an empty pane. The asked-for number, else the one open, else the first.
    const current = numbers?.find(n => n.id === selected) ?? numbers?.[0] ?? null;

    const open = (n: PhoneNumber) => {
        setSelected(n.id); setOpenCall(null); setLeft([]);
        writeQuery(n.id, null);
        showDetail(detailRef.current);
    };
    const openCallLog = (sid: string | null) => {
        setOpenCall(sid);
        writeQuery(current?.id ?? null, sid);
    };
    const changed = (n: PhoneNumber) => setNumbers(list => (list ?? []).map(x => (x.id === n.id ? n : x)));
    const added = (n: PhoneNumber) => {
        setNumbers(list => [...(list ?? []).filter(x => x.id !== n.id), n]);
        open(n);
    };
    const removed = (sentences: string[]) => {
        const gone = current?.id;
        setNumbers(list => (list ?? []).filter(x => x.id !== gone));
        setSelected(null); setOpenCall(null); setLeft(sentences);
        writeQuery(null, null);
    };

    const canAdd = host.canEdit && options.available;
    return (
        <host.Frame
            actions={
                <>
                    <IconButton icon={RefreshCw} label="Refresh" onClick={() => setReread(r => r + 1)} />
                    {host.canEdit && (
                        <Button onClick={() => setAdding(true)} disabled={!canAdd} data-phone-add>
                            <Plus className="size-4" aria-hidden /> Add a number
                        </Button>
                    )}
                </>
            }
        >
            <div className="grid gap-6">
                {!options.available && (
                    <div className="flex items-start gap-2 rounded-md border border-border bg-surface-2 px-3 py-2.5 text-[13px] text-text-muted" data-phone-unavailable>
                        <Info className="mt-0.5 size-4 shrink-0 text-text-faint" aria-hidden />
                        <p className="m-0">
                            {options.reason}
                            {host.links.address && (
                                <> <a href={host.links.address.href} className="text-accent-ink underline-offset-2 hover:underline">{host.links.address.label}</a></>
                            )}
                        </p>
                    </div>
                )}
                <ErrorNote>{loadError}</ErrorNote>
                {left.length > 0 && (
                    <div className="grid gap-2" data-phone-left>
                        {left.map(sentence => (
                            <p key={sentence} className="m-0 flex items-start gap-2 rounded-md border border-border bg-surface-2 px-3 py-2.5 text-[13px] text-text-muted">
                                <Info className="mt-0.5 size-4 shrink-0 text-text-faint" aria-hidden />{sentence}
                            </p>
                        ))}
                    </div>
                )}

                <div className={LIST_DETAIL}>
                    <nav aria-label="Numbers" className="min-w-0 [&>*]:min-w-0">
                        {numbers === null ? (
                            <p className="m-0 border-t border-border px-2.5 py-3 text-sm text-text-faint">Reading the numbers…</p>
                        ) : numbers.length === 0 ? (
                            <p className="m-0 border-t border-border px-2.5 py-3 text-sm text-text-faint">No numbers yet.</p>
                        ) : (
                            <ul className="m-0 list-none border-t border-border p-0">
                                {numbers.map(n => {
                                    const status = rowStatus(n);
                                    return (
                                        <ListRow
                                            key={n.id}
                                            data={{ 'data-phone-row': n.id }}
                                            leading={<Phone className="size-4 shrink-0 text-text-faint" aria-hidden />}
                                            title={n.name}
                                            sub={<span className="tabular-nums">{n.voice.number}</span>}
                                            meta={<span className={cn(status.tone)}>{status.word}</span>}
                                            current={current?.id === n.id}
                                            onClick={() => open(n)}
                                        />
                                    );
                                })}
                            </ul>
                        )}
                    </nav>

                    <section ref={detailRef} aria-label="Number" className="min-w-0 scroll-mt-4">
                        {current ? (
                            <NumberPane
                                key={current.id}
                                number={current}
                                agents={agents}
                                options={options}
                                openCall={openCall}
                                onOpenCall={openCallLog}
                                onChanged={changed}
                                onRemoved={removed}
                            />
                        ) : numbers !== null && (
                            <EmptyState
                                title="Let callers talk to your agents"
                                className="rounded-md border border-border"
                                action={host.canEdit && (
                                    <Button onClick={() => setAdding(true)} disabled={!canAdd}>
                                        <Plus className="size-4" aria-hidden /> Add a number
                                    </Button>
                                )}
                            >
                                Bring a number from your Twilio account. Someone who calls it talks to the agent you
                                choose, with the tools you allow, and every call is kept here with its transcript.
                                You need a Twilio API key and a number; it takes three steps.
                            </EmptyState>
                        )}
                    </section>
                </div>
            </div>

            <AddNumber
                open={adding}
                onClose={() => setAdding(false)}
                agents={agents}
                options={options}
                taken={Object.fromEntries((numbers ?? []).filter(n => n.voice.number_sid).map(n => [String(n.voice.number_sid), n.name]))}
                onConnected={added}
            />
        </host.Frame>
    );
}
