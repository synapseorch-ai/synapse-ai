'use client';

/*
 * A shared board's access list (C8): the agents, workflows and schedules that may use it, each adding cards or
 * reading, claiming and updating them. Deny by default: what is not named here never sees the board. An agent
 * takes its own board plus up to three shared ones, so the store refuses a fourth and its sentence shows here.
 */
import { useEffect, useMemo, useState } from 'react';
import { Plus, X } from 'lucide-react';

import { Button, Combobox, ErrorNote, IconButton, Select, type ComboboxOption } from '@/components/ui';

import { ACCESS_LABEL, KIND_NOUN, type Access, type AccessEntry, type PrincipalKind } from './board';
import { useBoardsHost, type Principal } from './host';

const ACCESS_OPTIONS = (Object.keys(ACCESS_LABEL) as Access[]).map(a => ({ value: a, label: ACCESS_LABEL[a] }));

export function AccessList({ entries, canEdit, onSave }: {
    entries: AccessEntry[];
    canEdit: boolean;
    /** Replace the list; rejects with the store's sentence. */
    onSave: (entries: { kind: PrincipalKind; id: string; access: Access }[]) => Promise<void>;
}) {
    const { loadPrincipals } = useBoardsHost();
    const [draft, setDraft] = useState(entries);
    const [principals, setPrincipals] = useState<Principal[]>([]);
    const [pick, setPick] = useState('');
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    useEffect(() => { setDraft(entries); }, [entries]);
    useEffect(() => { if (canEdit) loadPrincipals().then(setPrincipals).catch(() => setPrincipals([])); }, [canEdit, loadPrincipals]);

    const named = new Set(draft.map(e => `${e.kind}:${e.id}`));
    const options: ComboboxOption[] = useMemo(() => principals
        .filter(p => !named.has(`${p.kind}:${p.id}`))
        .map(p => ({ value: `${p.kind}:${p.id}`, label: p.name, group: `${KIND_NOUN[p.kind]}s` })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [principals, draft]);
    const dirty = JSON.stringify(draft.map(e => [e.kind, e.id, e.access])) !== JSON.stringify(entries.map(e => [e.kind, e.id, e.access]));

    const add = (value: string) => {
        const p = principals.find(x => `${x.kind}:${x.id}` === value);
        if (!p) return;
        setDraft(d => [...d, { kind: p.kind, id: p.id, name: p.name, access: 'work', gone: false }]);
        setPick('');
    };
    const save = async () => {
        setBusy(true);
        try {
            await onSave(draft.map(({ kind, id, access }) => ({ kind, id, access })));
            setError('');
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <section className="space-y-3" aria-labelledby="board-access">
            <div>
                <h4 id="board-access" className="text-xs font-semibold uppercase tracking-wider text-text-faint">Who can use this board</h4>
                <p className="mt-1 text-xs text-text-faint">
                    Only what is named here sees the board. An agent, workflow or schedule takes at most 3 shared boards; an
                    agent also gets the boards of the workflow or schedule it runs in.
                </p>
            </div>
            {draft.length === 0 && <p className="text-sm text-text-muted">Nobody yet: no agent sees this board.</p>}
            <ul className="space-y-1">
                {draft.map((e, i) => (
                    <li key={`${e.kind}:${e.id}`} className="flex min-h-9 items-center gap-2">
                        <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm text-text">{e.gone ? `${e.name} (deleted)` : e.name}</span>
                            <span className="block text-xs text-text-faint">{KIND_NOUN[e.kind]}</span>
                        </span>
                        {canEdit ? (
                            <>
                                <Select aria-label={`What ${e.name} may do`} value={e.access} options={ACCESS_OPTIONS}
                                    onChange={(v: string) => setDraft(d => d.map((x, j) => j === i ? { ...x, access: v as Access } : x))} />
                                <IconButton label={`Remove ${e.name}`} icon={X} size="sm"
                                    onClick={() => setDraft(d => d.filter((_, j) => j !== i))} />
                            </>
                        ) : <span className="text-xs text-text-muted">{ACCESS_LABEL[e.access]}</span>}
                    </li>
                ))}
            </ul>
            {canEdit && (
                <div className="flex flex-wrap items-center gap-2">
                    <div className="min-w-48 flex-1">
                        <Combobox aria-label="Add an agent, workflow or schedule" placeholder="Add an agent, workflow or schedule"
                            options={options} value={pick} onChange={(v: string) => { setPick(v); add(v); }} />
                    </div>
                    <Button size="sm" disabled={busy || !dirty} onClick={save}><Plus className="size-3.5" aria-hidden />Save the list</Button>
                </div>
            )}
            {error && <ErrorNote>{error}</ErrorNote>}
        </section>
    );
}
