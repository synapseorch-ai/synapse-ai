'use client';

/**
 * The transform step's Python editor — `CodeEditor` with the language fixed.
 *
 * It was a CodeMirror instance of its own, one-dark in both themes on purpose.
 * The kit's editor follows the theme from tokens now, so the dark block in a
 * light form is gone, and the two copies of the setup became one. Kept as a
 * name because both products import it from here.
 */

import { CodeEditor, type CodeEditorProps } from '@/components/ui';

export function PythonEditor({
    value,
    onChange,
    ...props
}: { value: string; onChange: (code: string) => void } & Omit<CodeEditorProps, 'language' | 'value' | 'onChange'>) {
    return <CodeEditor {...props} language="python" value={value} onChange={onChange} />;
}
