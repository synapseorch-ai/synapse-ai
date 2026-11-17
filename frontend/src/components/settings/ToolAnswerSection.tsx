/**
 * "What it answers": a custom tool's output schema, edited as answer fields (F1620). The rows, the pastes and the
 * schema they compile to are the model step's editor (F1618 d's `AnswerFieldsEditor`), in a tool's words. What the
 * tool declares is checked where a workflow reads its answer, before it runs, and held at run time.
 *
 * The saved text changes only when a field does: a schema inside the answer's subset is saved as it was until then,
 * and one with a part outside the subset is shown read-only, each part named, and saved as it is unless an example or
 * a JSON Schema is pasted in its place.
 */
import { useState } from 'react';
import { Label } from '@/components/ui';
import type { AnswerField } from '@/components/orchestration/answerFields';
import {
    AnswerFieldsEditor, AnswerPaste, AnswerPasteButtons, type AnswerFieldsWords,
} from '@/components/orchestration/panel/answerFieldsEditor';
import { toolAnswerOf, toolAnswerText } from './toolAnswer';

// Headed in the tool editor's own label style, as its other fields are.
const WORDS: AnswerFieldsWords = {
    testId: 'tool-answer-fields',
    intro: 'The fields this tool answers. A workflow that reads a field it does not answer is told before it runs, and a '
        + 'run says when an answer leaves one out.',
    add: 'Add a field',
    addType: 'string',
    shown: {
        title: 'Its output schema',
        intro: 'The JSON Schema saved with this tool, from the fields above. To change it, change the fields.',
        label: "The tool's output schema",
    },
};

export function ToolAnswerSection({ text, onChange }: { text?: string; onChange: (text: string) => void }) {
    const [first] = useState(() => toolAnswerOf(text));
    const [fields, setFields] = useState<AnswerField[]>(first.fields);
    const [kept, setKept] = useState<string[]>(first.kept);
    const [paste, setPaste] = useState<null | 'example' | 'schema'>(null);
    const set = (next: AnswerField[]) => {
        // A row committed as it was changes nothing, so the saved schema is never rewritten by a visit.
        if (JSON.stringify(next) === JSON.stringify(fields)) return;
        setFields(next);
        setKept([]);
        onChange(toolAnswerText(next));
    };
    return (
        <div className="space-y-1" data-testid="tool-answer">
            <Label className="block">
                What it answers <span className="text-text-faint font-normal normal-case">(optional)</span>
            </Label>
            {kept.length ? <KeptAnswer kept={kept} text={text} paste={paste} setPaste={setPaste} take={set} />
                : <AnswerFieldsEditor fields={fields} onChange={set} words={WORDS} />}
        </div>
    );
}

/** A schema the rows cannot show (F1620): kept as it is, read-only, each part named; a paste takes its place. */
function KeptAnswer({ kept, text, paste, setPaste, take }: {
    kept: string[]; text?: string; paste: null | 'example' | 'schema';
    setPaste: (next: null | 'example' | 'schema') => void; take: (next: AnswerField[]) => void;
}) {
    return (
        <div className="space-y-1.5">
            <div data-testid="tool-answer-kept" className="space-y-1 rounded-md border border-border bg-surface-2/40 p-2 text-xs text-text">
                <p className="font-medium">
                    This tool&apos;s output schema has {kept.length === 1 ? 'a part' : 'parts'} the rows cannot show, so it
                    is kept as it is and saved unchanged:
                </p>
                <ul className="list-disc space-y-0.5 pl-4">
                    {kept.map((why, i) => <li key={i}>{why}</li>)}
                </ul>
                <p className="text-text-muted">To edit it as fields, paste an example or a JSON Schema in its place.</p>
            </div>
            <details className="rounded-md border border-border">
                <summary className="cursor-pointer px-2 py-1.5 text-xs font-medium text-text">{WORDS.shown.title}</summary>
                <pre
                    tabIndex={0}
                    aria-label={WORDS.shown.label}
                    className="max-h-72 overflow-auto px-2 pb-2 pt-1 font-code text-[11px] leading-snug text-text"
                >
                    {text}
                </pre>
            </details>
            <div className="flex flex-wrap gap-1.5">
                <AnswerPasteButtons paste={paste} setPaste={setPaste} />
            </div>
            {paste && (
                <AnswerPaste
                    key={paste}
                    kind={paste}
                    replacing={0}
                    onTake={(next) => { setPaste(null); take(next); }}
                    onCancel={() => setPaste(null)}
                />
            )}
        </div>
    );
}
