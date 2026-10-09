'use client';

/*
 * A run's transcript, read on screen (cloud's viewer, shared since M2h).
 *
 * The engine writes these as terminal-styled text — banner rules, box glyphs,
 * emoji section markers — because they were made to be `cat`-ed in a terminal.
 * Dropped into a `<pre>` that formatting survives but its *structure* does not:
 * 24KB of uniform grey in which "what did the model actually see on turn 2" is
 * a scroll and a squint away.
 *
 * So this reads the markers the logger already emits and does two things with
 * them: tints the line, and builds an outline. Nothing is rewritten — the text
 * on screen is byte-for-byte the file, which matters because this is the view
 * people open when they do not trust what happened.
 *
 * The markers are matched loosely (an emoji plus a keyword): `agent_logger.py`
 * and `orchestration/logger.py` compose these lines with f-strings and phrase
 * some differently, and a marker that stops matching costs a tint and an outline
 * row rather than the log.
 *
 * A context compaction (OSS's agents write one when a conversation outgrows the
 * model) is a block from a `====` rule through "CONTEXT COMPACTED" to the next
 * rule; its lines sit on the machine's ground so it reads as one box, and it is
 * an outline entry.
 *
 * Each product reads the text its own way (`load`) and throws the sentence to
 * show when it can't.
 */
import { Check, Copy } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';

import { Button, ErrorNote } from '@/components/ui';
import { cn } from '@/lib/cn';

/** First match wins, so the specific markers sit above the generic rule lines. */
const MARKERS: { test: (line: string) => boolean; tone: string; outline: boolean }[] = [
    { test: (l) => l.includes('🔄') && l.includes('LLM CALL'), tone: 'text-accent-ink', outline: true },
    { test: (l) => l.includes('▶') && l.includes('STEP'), tone: 'text-accent-ink', outline: true },
    { test: (l) => l.includes('🔧') && l.includes('TOOL CALL'), tone: 'text-warning', outline: true },
    { test: (l) => l.includes('📤') && l.includes('TOOL RESULT'), tone: 'text-text-muted', outline: false },
    { test: (l) => l.includes('🧠'), tone: 'text-success', outline: false },
    { test: (l) => l.includes('❌') || /\bERROR\b/.test(l), tone: 'text-danger', outline: true },
    { test: (l) => /^\s*(SYSTEM PROMPT|CONTEXT \/ PROMPT SENT|📝 INPUT PROMPT|🗺)/.test(l), tone: 'text-text', outline: true },
    { test: (l) => /^\s*(══|──|═|─){10,}/.test(l), tone: 'text-border-strong', outline: false },
];

function classify(line: string): { tone: string; outline: boolean } {
    for (const marker of MARKERS) {
        if (marker.test(line)) return { tone: marker.tone, outline: marker.outline };
    }
    return { tone: 'text-text-muted', outline: false };
}

/** The banner rules read as structure on a terminal and as noise in an outline. */
function outlineLabel(line: string): string {
    return line.replace(/[│─═▶=]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 80);
}

/** The lines inside each CONTEXT COMPACTED block, and each block's first line. */
export function compactedLines(text: string): { inside: Set<number>; starts: Set<number> } {
    const inside = new Set<number>();
    const starts = new Set<number>();
    const re = /={10,}[\s\S]*?CONTEXT COMPACTED[\s\S]*?={10,}/g;
    const lineAt = (offset: number) => text.slice(0, offset).split('\n').length - 1;
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
        const first = lineAt(match.index);
        const last = lineAt(match.index + match[0].length);
        starts.add(first);
        for (let i = first; i <= last; i++) inside.add(i);
    }
    return { inside, starts };
}

export function Transcript({
    id, load, bodyClassName, className,
}: {
    /** What the text is (a run id): a new id reads again. */
    id: string;
    /** Read the text; throw an Error whose message is the sentence to show. */
    load: (signal: AbortSignal) => Promise<string>;
    /** The text box's height (default `max-h-[32rem]`). */
    bodyClassName?: string;
    className?: string;
}) {
    // What was read, and for which id: anything older than `id` is still loading.
    const [read, setRead] = useState<{ id: string; text: string | null; error: string } | null>(null);
    const [copied, setCopied] = useState(false);
    const bodyRef = useRef<HTMLPreElement>(null);
    // The newest `load`, without reading again each time a parent re-renders.
    const loadRef = useRef(load);
    useEffect(() => { loadRef.current = load; });

    useEffect(() => {
        const controller = new AbortController();
        loadRef.current(controller.signal)
            .then((body) => { if (!controller.signal.aborted) setRead({ id, text: body, error: '' }); })
            .catch((e: unknown) => {
                if (!controller.signal.aborted) {
                    setRead({ id, text: null, error: e instanceof Error ? e.message : 'The transcript could not be read.' });
                }
            });
        return () => controller.abort();
    }, [id]);

    const loading = read?.id !== id;
    const text = loading ? null : read.text;
    const error = loading ? '' : read.error;

    const lines = useMemo(() => (text ?? '').split('\n'), [text]);
    const classified = useMemo(() => lines.map(classify), [lines]);
    const compacted = useMemo(() => compactedLines(text ?? ''), [text]);
    const outline = useMemo(
        () => lines
            .map((line, index) => ({ index, line }))
            .filter(({ index }) => compacted.starts.has(index) || (!compacted.inside.has(index) && classified[index].outline))
            .map(({ index, line }) => ({
                index,
                label: compacted.starts.has(index) ? 'Context compacted' : outlineLabel(line),
            }))
            .filter((entry) => entry.label.length > 0),
        [lines, classified, compacted],
    );

    function jump(index: number) {
        const target = bodyRef.current?.querySelector<HTMLElement>(`[data-line="${index}"]`);
        target?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }

    async function copy() {
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch {
            // No clipboard (an insecure origin): the text is still on screen to select.
        }
    }

    if (loading) return <p className="text-sm text-text-muted">Reading the transcript…</p>;
    if (error) return <ErrorNote>{error}</ErrorNote>;
    if (text === null) return null;

    return (
        <div data-transcript className={cn('min-w-0 space-y-3', className)}>
            <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs tabular-nums text-text-faint">
                    {lines.length.toLocaleString()} lines · {(text.length / 1024).toFixed(1)} KB
                </span>
                <Button variant="secondary" size="sm" onClick={copy}>
                    {copied ? <Check className="size-4" aria-hidden /> : <Copy className="size-4" aria-hidden />}
                    {copied ? 'Copied' : 'Copy'}
                </Button>
            </div>

            {/* Every model call, step and tool call in order, as a jump list. */}
            {outline.length > 1 && (
                <nav aria-label="Transcript sections" className="max-h-40 overflow-y-auto rounded-md border border-border p-1.5">
                    <ol className="m-0 list-none space-y-0.5 p-0">
                        {outline.map((entry) => (
                            <li key={entry.index} className="m-0">
                                <button
                                    type="button"
                                    onClick={() => jump(entry.index)}
                                    className="block w-full truncate rounded px-2 py-0.5 text-left font-code leading-5 text-2xs text-text-muted transition-colors hover:bg-surface-2 hover:text-text"
                                >
                                    {entry.label}
                                </button>
                            </li>
                        ))}
                    </ol>
                </nav>
            )}

            {/*
              `whitespace-pre`, not `pre-wrap`: the logger aligns its banners with
              spaces, and wrapping a 100-column rule at the pane's width turns every
              section head into two ragged lines. It scrolls sideways instead.
            */}
            <pre
                ref={bodyRef}
                tabIndex={0}
                aria-label="Transcript"
                className={cn(
                    'scroll-x m-0 overflow-y-auto whitespace-pre rounded-md border border-border bg-bg px-4 py-3 font-code text-xs leading-relaxed',
                    bodyClassName ?? 'max-h-[32rem]',
                )}
            >
                {lines.map((line, index) => {
                    const boxed = compacted.inside.has(index);
                    return (
                        <span
                            key={index}
                            data-line={index}
                            data-compacted={boxed || undefined}
                            className={cn(
                                'block scroll-mt-3',
                                boxed ? '-mx-4 bg-machine-subtle px-4 text-accent-ink' : classified[index].tone,
                            )}
                        >
                            {line || ' '}
                        </span>
                    );
                })}
            </pre>
        </div>
    );
}
