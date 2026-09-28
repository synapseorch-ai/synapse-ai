'use client';

/**
 * What an agent has remembered — and the way to correct or remove it (F270).
 *
 * OSS ran the memory engine with nothing in the product to show it: a person
 * could not see, correct or delete what an agent remembered except by asking the
 * agent to `forget_memory`. Cloud's viewer (`web/src/app/org/agents/AgentMemory.tsx`)
 * reads it; this is that viewer, plus edit and delete.
 *
 * `MEMORY.md` is the index — one line per memory, riding the agent's prompt on
 * every turn. Saved memories and session traces are the body, listed newest
 * first (the order `list_memories` gives the agent) and fetched one at a time.
 * An edit is saved as the newest memory and re-indexed; the index line is the
 * memory's first line, so it is corrected by editing the memory itself.
 *
 * Loaded when opened, behind a `Section`: the editor is where an agent is
 * changed, and its memory is something inspected now and then.
 */

import { Brain, FileText, MessageSquare, Pencil, Trash2 } from 'lucide-react';
import * as React from 'react';

import { LocalTime } from '@/components/LocalTime';
import { Button, ConfirmDialog, ErrorNote, Markdown, Modal, Muted, Section, Textarea } from '@/components/ui';

type Item = { name: string; size: number; summary: string; archived: boolean };
type Session = { name: string; size: number; when: string | null; first: string };

type MemoryView = {
  enabled: boolean;
  index: string;
  index_lines: number;
  archive_lines: number;
  items: Item[];
  sessions: Session[];
};

type Kind = 'items' | 'sessions';
type Open = { kind: Kind; name: string; content: string; editing: boolean };
type Doomed = { kind: Kind; name: string } | { kind: 'all' };

function kb(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

async function detailOf(res: Response, fallback: string): Promise<string> {
  const detail = (await res.json().catch(() => ({}))).detail;
  return typeof detail === 'string' ? detail : fallback;
}

export function AgentMemory({ agentId }: { agentId: string }) {
  const [view, setView] = React.useState<MemoryView | null>(null);
  const [error, setError] = React.useState('');
  const [loading, setLoading] = React.useState(false);
  const [open, setOpen] = React.useState<Open | null>(null);
  const [draft, setDraft] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [doomed, setDoomed] = React.useState<Doomed | null>(null);
  const requested = React.useRef(false);
  const base = `/api/agents/${encodeURIComponent(agentId)}/memory`;

  const load = React.useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch(base);
      if (!res.ok) throw new Error(await detailOf(res, 'That memory could not be read.'));
      setView(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : 'That memory could not be read.');
      requested.current = false; // opening it again tries again
    } finally {
      setLoading(false);
    }
  }, [base]);

  // The first open reads it once — an effect can run twice on mount; after an
  // edit or a delete, `load` reads it again on purpose.
  const loadOnce = React.useCallback(() => {
    if (requested.current) return;
    requested.current = true;
    void load();
  }, [load]);

  async function openFile(kind: Kind, name: string) {
    setError('');
    const res = await fetch(`${base}/${kind}/${encodeURIComponent(name)}`);
    if (!res.ok) {
      setError(await detailOf(res, 'That file could not be read.'));
      return;
    }
    const file = (await res.json()) as { content: string };
    setOpen({ kind, name, content: file.content, editing: false });
    setDraft(file.content);
  }

  async function save() {
    if (!open) return;
    setBusy(true);
    try {
      const res = await fetch(`${base}/items/${encodeURIComponent(open.name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: draft }),
      });
      if (!res.ok) {
        setError(await detailOf(res, 'That memory could not be saved.'));
        return;
      }
      setOpen(null);
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!doomed) return;
    setBusy(true);
    try {
      const url = doomed.kind === 'all' ? base : `${base}/${doomed.kind}/${encodeURIComponent(doomed.name)}`;
      const res = await fetch(url, { method: 'DELETE' });
      if (!res.ok) {
        setError(await detailOf(res, 'That could not be deleted.'));
        return;
      }
      setDoomed(null);
      setOpen(null);
      await load();
    } finally {
      setBusy(false);
    }
  }

  const total = (view?.sessions.length ?? 0) + (view?.items.length ?? 0);

  return (
    <Section
      title="What it remembers"
      description="The index that rides every prompt, the memories behind it, and past sessions."
      summary={view ? (total === 0 ? 'Nothing yet' : `${total} files`) : 'Open to load'}
    >
      <Opener onOpen={loadOnce} />

      {loading && !view && <Muted className="block">Reading…</Muted>}
      <ErrorNote>{error}</ErrorNote>

      {view && (
        <div className="space-y-5">
          {!view.enabled && total > 0 && (
            <Muted className="block">
              Memory is off for this agent, so none of this is read or added to. It is kept until you delete it.
            </Muted>
          )}

          <div>
            <h4 className="mb-1.5 flex items-center gap-1.5 text-2xs font-medium uppercase tracking-wider text-text-faint">
              <Brain className="size-3" aria-hidden /> Index · MEMORY.md
            </h4>
            {view.index.trim() ? (
              <div className="rounded-md border border-border bg-bg p-3">
                <Markdown>{view.index}</Markdown>
              </div>
            ) : (
              <Muted className="block">
                Nothing indexed yet. The agent adds a line here when it calls{' '}
                <code className="font-code">remember</code>; session traces below are written
                automatically and do not need one.
              </Muted>
            )}
            {view.archive_lines > 0 && (
              <Muted className="mt-1.5 block">
                {view.archive_lines} older {view.archive_lines === 1 ? 'line is' : 'lines are'} archived past the
                index&apos;s cap: still searchable, no longer in the prompt. Their memories are listed below.
              </Muted>
            )}
          </div>

          <FileList
            icon={FileText}
            label="Saved memories"
            empty="None. These appear when the agent calls remember."
            rows={view.items.map((item) => ({
              name: item.name,
              size: item.size,
              detail: item.archived ? `${item.summary} · archived` : item.summary,
            }))}
            onOpen={(name) => openFile('items', name)}
            onDelete={(name) => setDoomed({ kind: 'items', name })}
          />

          <FileList
            icon={MessageSquare}
            label="Sessions"
            empty="No conversations recorded yet."
            rows={view.sessions.map((s) => ({ name: s.name, size: s.size, detail: s.first, when: s.when }))}
            onOpen={(name) => openFile('sessions', name)}
            onDelete={(name) => setDoomed({ kind: 'sessions', name })}
          />

          {total > 0 && (
            <div>
              <Button variant="danger" size="sm" onClick={() => setDoomed({ kind: 'all' })}>
                <Trash2 className="size-3.5" aria-hidden /> Forget everything
              </Button>
            </div>
          )}
        </div>
      )}

      <Modal
        open={open !== null}
        onClose={() => setOpen(null)}
        title={open?.name ?? ''}
        size="lg"
        footer={
          open?.editing ? (
            <>
              <Button variant="secondary" onClick={() => setOpen({ ...open, editing: false })} disabled={busy}>
                Cancel
              </Button>
              <Button onClick={save} loading={busy} disabled={!draft.trim()}>
                Save
              </Button>
            </>
          ) : (
            <>
              {open?.kind === 'items' && (
                <Button variant="secondary" onClick={() => setOpen({ ...open, editing: true })}>
                  <Pencil className="size-3.5" aria-hidden /> Edit
                </Button>
              )}
              <Button variant="secondary" onClick={() => setOpen(null)}>
                Close
              </Button>
            </>
          )
        }
      >
        {open && (open.editing ? (
          <Textarea
            aria-label={`The memory ${open.name}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={14}
            className="font-code text-xs"
          />
        ) : (
          /* Pre-formatted, not markdown: a session trace is a timestamped list the
             engine wrote to a fixed shape, and bullets lose its alignment. */
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-md border border-border bg-bg p-3 font-code text-xs leading-relaxed text-text-muted">
            {open.content}
          </pre>
        ))}
      </Modal>

      <ConfirmDialog
        open={doomed !== null}
        onClose={() => setDoomed(null)}
        onConfirm={remove}
        busy={busy}
        title={doomed?.kind === 'all' ? 'Forget everything this agent remembers?' : `Delete ${doomed && 'name' in doomed ? doomed.name : ''}?`}
        description={
          doomed?.kind === 'all'
            ? 'Every saved memory, the index and every session trace are deleted. The agent starts with no memory.'
            : doomed?.kind === 'items'
              ? 'The memory and its index line are deleted; the agent no longer knows it.'
              : 'This session trace is deleted; the agent can no longer look it up.'
        }
        confirmLabel={doomed?.kind === 'all' ? 'Forget everything' : 'Delete'}
      />
    </Section>
  );
}

type Row = { name: string; size: number; detail: string; when?: string | null };

function FileList({
  icon: Icon,
  label,
  empty,
  rows,
  onOpen,
  onDelete,
}: {
  icon: React.ElementType;
  label: string;
  empty: string;
  rows: Row[];
  onOpen: (name: string) => void;
  onDelete: (name: string) => void;
}) {
  return (
    <div>
      <h4 className="mb-1.5 flex items-center gap-1.5 text-2xs font-medium uppercase tracking-wider text-text-faint">
        <Icon className="size-3" aria-hidden /> {label}
        {rows.length > 0 && <span className="text-text-faint">· {rows.length}</span>}
      </h4>
      {rows.length === 0 ? (
        <Muted className="block">{empty}</Muted>
      ) : (
        <ul className="max-h-80 divide-y divide-border overflow-y-auto rounded-md border border-border">
          {rows.map((row) => (
            <li key={row.name} className="flex items-center">
              <button
                type="button"
                onClick={() => onOpen(row.name)}
                className="flex min-w-0 flex-1 items-center gap-3 px-3 py-2 text-left transition-colors hover:bg-surface-2/50"
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-code text-xs text-text" translate="no">{row.name}</span>
                  {row.detail && <span className="block truncate text-2xs text-text-faint">{row.detail}</span>}
                </span>
                <span className="shrink-0 text-2xs tabular-nums text-text-faint">{kb(row.size)}</span>
                {row.when !== undefined && (
                  <span className="shrink-0 text-2xs text-text-faint">
                    <LocalTime iso={row.when} format="medium" />
                  </span>
                )}
              </button>
              <button
                type="button"
                onClick={() => onDelete(row.name)}
                aria-label={`Delete ${row.name}`}
                title="Delete"
                className="shrink-0 px-3 py-2 text-text-faint transition-colors hover:text-danger"
              >
                <Trash2 className="size-3.5" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Fires `onOpen` the first time it is mounted, which `Section` only does when
 * the panel is expanded — so "load on open" is a property of mounting.
 */
function Opener({ onOpen }: { onOpen: () => void }) {
  React.useEffect(() => {
    onOpen();
  }, [onOpen]);
  return null;
}
