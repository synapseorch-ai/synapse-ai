/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';

import { useEffect, useRef, useState, useCallback } from 'react';
import { Play, ChevronDown, ChevronUp, Terminal, Package, AlertCircle, CheckCircle2 } from 'lucide-react';
import { EditorView } from '@codemirror/view';
import { basicSetup } from 'codemirror';
import { python } from '@codemirror/lang-python';
import { oneDarkTheme } from '@codemirror/theme-one-dark';
import { EditorState } from '@codemirror/state';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';

import { Spinner } from '@/components/ui';
/* ─── Types ──────────────────────────────────────────────────────────────── */

export interface PythonDraftTool {
    name: string;
    generalName: string;
    description: string;
    tool_type: 'python';
    code: string;
    inputSchema: any;
    schemaParams?: any[];
}

interface PythonToolEditorProps {
    draft: PythonDraftTool;
    onChange: (updated: PythonDraftTool) => void;
}

/* ─── Rich Python syntax highlighting ────────────────────────────────────── */

const pythonHighlightStyle = HighlightStyle.define([
    // Keywords: if, else, for, while, return, import, from, as, with, pass, break, continue, yield, lambda, del, global, nonlocal, raise, try, except, finally, assert
    { tag: tags.keyword, color: '#c792ea', fontWeight: 'bold' },
    // def / class keywords
    { tag: tags.definitionKeyword, color: '#c792ea', fontWeight: 'bold' },
    // self, True, False, None
    { tag: tags.self, color: '#f78c6c', fontStyle: 'italic' },
    { tag: tags.bool, color: '#ff9cac' },
    { tag: tags.null, color: '#ff9cac' },
    // Function / method names at definition (def foo:)
    { tag: tags.definition(tags.function(tags.variableName)), color: '#82aaff', fontWeight: 'bold' },
    // Function calls  (foo())
    { tag: tags.function(tags.variableName), color: '#82aaff' },
    // Class names
    { tag: tags.definition(tags.className), color: '#ffcb6b', fontWeight: 'bold' },
    { tag: tags.className, color: '#ffcb6b' },
    // Decorator (@decorator)
    { tag: tags.meta, color: '#ffa759', fontStyle: 'italic' },
    // Variables / property access
    { tag: tags.variableName, color: '#eeffff' },
    { tag: tags.propertyName, color: '#89ddff' },
    // Strings
    { tag: tags.string, color: '#c3e88d' },
    { tag: tags.special(tags.string), color: '#c3e88d' },  // f-strings, r-strings
    // Numbers
    { tag: tags.number, color: '#f78c6c' },
    // Operators  + - * / = == != < > etc.
    { tag: tags.operator, color: '#89ddff' },
    // Punctuation / brackets
    { tag: tags.punctuation, color: '#89ddff' },
    { tag: tags.bracket, color: '#ffcb6b' },
    // Comments
    { tag: tags.comment, color: '#546e7a', fontStyle: 'italic' },
    // Built-in attributes / special names
    { tag: tags.special(tags.variableName), color: '#80cbc4' },
    // Type annotations
    { tag: tags.typeName, color: '#ffcb6b' },
    // Import paths
    { tag: tags.moduleKeyword, color: '#c792ea', fontWeight: 'bold' },
    // Escape sequences in strings
    { tag: tags.escape, color: '#f78c6c' },
]);

const richPythonTheme = syntaxHighlighting(pythonHighlightStyle);

/* ─── Sandbox packages info ───────────────────────────────────────────────── */

const PACKAGES = [
    'pandas', 'numpy', 'scipy', 'scikit-learn', 'matplotlib',
    'seaborn', 'requests', 'httpx', 'beautifulsoup4', 'lxml',
    'openpyxl', 'xlsxwriter', 'pyyaml', 'tabulate', 'jinja2',
    'jsonschema', 'pillow', 'sympy',
];

const DEFAULT_CODE = `# _args contains all tool arguments as a Python dict
# Use print() — stdout becomes the tool result
# JSON output is recommended: the agent can parse it
import json

query = _args.get("query", "")

# Your logic here
result = {"output": f"Processed: {query}"}

print(json.dumps(result))
`;

/* ─── Simple Python lint (client-side, best-effort) ─────────────────────── */

interface LintWarning {
    line: number;
    message: string;
    severity: 'error' | 'warning';
}

function lintPython(code: string): LintWarning[] {
    const warnings: LintWarning[] = [];
    const lines = code.split('\n');
    lines.forEach((line, i) => {
        const lnum = i + 1;
        // Syntax: unmatched quotes
        const singleQ = (line.match(/(?<!\\)'/g) || []).filter(q => !line.includes("'''")).length;
        const doubleQ = (line.match(/(?<!\\)"/g) || []).filter(q => !line.includes('"""')).length;
        if (singleQ % 2 !== 0 || doubleQ % 2 !== 0) {
            warnings.push({ line: lnum, message: 'Possible unmatched quote', severity: 'warning' });
        }
        // Use of undefined _args check hint
        if (/\barguments\b/.test(line) && !/\b_args\b/.test(lines.join('\n'))) {
            warnings.push({ line: lnum, message: 'Did you mean `_args` instead of `arguments`?', severity: 'warning' });
        }
        // print statement Python 2 style
        if (/^\s*print\s+[^(]/.test(line)) {
            warnings.push({ line: lnum, message: 'Python 2 print statement — use print() instead', severity: 'error' });
        }
    });
    return warnings;
}

/* ─── Component ──────────────────────────────────────────────────────────── */

export function PythonToolEditor({ draft, onChange }: PythonToolEditorProps) {
    const editorContainerRef = useRef<HTMLDivElement>(null);
    const editorViewRef = useRef<EditorView | null>(null);
    const [showPackages, setShowPackages] = useState(false);
    const [testArgs, setTestArgs] = useState('{}');
    const [testResult, setTestResult] = useState<{
        stdout: string; stderr: string; exit_code: number;
    } | null>(null);
    const [testLoading, setTestLoading] = useState(false);
    const [testError, setTestError] = useState<string | null>(null);
    const [lintWarnings, setLintWarnings] = useState<LintWarning[]>([]);

    /* ── Init CodeMirror ──────────────────────────────────────────────────── */

    const onDocChange = useCallback((code: string) => {
        const warnings = lintPython(code);
        setLintWarnings(warnings);
        onChange({ ...draft, code });
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [draft, onChange]);

    useEffect(() => {
        if (!editorContainerRef.current) return;
        if (editorViewRef.current) return; // already mounted

        const startState = EditorState.create({
            doc: draft.code || DEFAULT_CODE,
            extensions: [
                basicSetup,
                python(),
                oneDarkTheme,
                richPythonTheme,
                EditorView.updateListener.of((update: import('@codemirror/view').ViewUpdate) => {
                    if (update.docChanged) {
                        onDocChange(update.state.doc.toString());
                    }
                }),
                EditorView.theme({
                    '&': {
                        backgroundColor: '#09090b',
                        height: '100%',
                        borderRadius: '0',
                    },
                    '.cm-scroller': { overflow: 'auto', fontFamily: 'monospace', fontSize: '13px' },
                    '.cm-content': { padding: '12px 0' },
                    '.cm-line': { padding: '0 16px' },
                    '&.cm-focused .cm-cursor': { borderLeftColor: '#a855f7' },
                    '.cm-selectionBackground': { backgroundColor: '#3f3f46' },
                    '&.cm-focused .cm-selectionBackground': { backgroundColor: '#4f46e5' },
                }),
            ],
        });

        editorViewRef.current = new EditorView({
            state: startState,
            parent: editorContainerRef.current,
        });

        // Initial lint
        setLintWarnings(lintPython(draft.code || DEFAULT_CODE));

        return () => {
            editorViewRef.current?.destroy();
            editorViewRef.current = null;
        };
        // Only run on mount
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    /* ── Schema JSON state ───────────────────────────────────────────────── */

    const [schemaStr, setSchemaStr] = useState(() =>
        JSON.stringify(draft.inputSchema || { type: 'object', properties: { input: { type: 'string' } } }, null, 2)
    );
    const [schemaError, setSchemaError] = useState<string | null>(null);

    const handleSchemaChange = (val: string) => {
        setSchemaStr(val);
        try {
            const parsed = JSON.parse(val);
            setSchemaError(null);
            onChange({ ...draft, inputSchema: parsed });
        } catch {
            setSchemaError('Invalid JSON');
        }
    };

    /* ── Test run ─────────────────────────────────────────────────────────── */

    const handleTestRun = async () => {
        if (!draft.code.trim()) return;
        setTestLoading(true);
        setTestResult(null);
        setTestError(null);
        let args: any = {};
        try {
            args = JSON.parse(testArgs || '{}');
        } catch {
            setTestError('Test Args must be valid JSON');
            setTestLoading(false);
            return;
        }
        try {
            const res = await fetch('/api/tools/python/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: draft.code, args, timeout: 30 }),
            });
            if (!res.ok) {
                const err = await res.json();
                setTestError(err.detail || 'Test run failed');
                return;
            }
            const data = await res.json();
            setTestResult(data);
        } catch (e: any) {
            setTestError(e.message || 'Network error');
        } finally {
            setTestLoading(false);
        }
    };

    /* ── Render ─────────────────────────────────────────────────────────────── */

    return (
        <div className="flex flex-col gap-5">

            {/* ── Info Banner ─────────────────────────────────────────────── */}
            <div className="bg-zinc-950 border border-accent/40 rounded-sm p-3 space-y-2">
                <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2 text-xs font-bold text-accent uppercase tracking-wider">
                        <Package className="h-3.5 w-3.5" />
                        Python Sandbox
                    </div>
                    <button
                        onClick={() => setShowPackages(v => !v)}
                        className="text-[10px] text-zinc-500 hover:text-zinc-300 flex items-center gap-1"
                    >
                        {showPackages ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                        {showPackages ? 'Hide' : 'Show'} packages
                    </button>
                </div>
                <p className="text-[11px] text-zinc-500 leading-relaxed">
                    Code runs in a <span className="text-accent font-mono">Docker sandbox</span> (512MB RAM, 1 CPU, internet enabled).
                    Vault files are mounted at <span className="font-mono text-accent">/data/</span>.
                    Arguments are injected as <span className="font-mono text-accent">_args</span> (dict).
                    <span className="font-mono text-zinc-400"> print()</span> stdout becomes the tool result.
                </p>
                {showPackages && (
                    <div className="flex flex-wrap gap-1.5 pt-1 border-t border-zinc-800">
                        {PACKAGES.map(pkg => (
                            <span key={pkg} className="text-[10px] font-mono bg-zinc-900 border border-zinc-700 text-zinc-400 px-1.5 py-0.5 rounded-md">
                                {pkg}
                            </span>
                        ))}
                    </div>
                )}
            </div>

            {/* ── Code Editor ─────────────────────────────────────────────── */}
            <div className="space-y-1">
                <div className="flex items-center justify-between">
                    <label className="text-[10px] uppercase font-bold text-zinc-500">Python Code</label>
                    {lintWarnings.length > 0 && (
                        <div className="flex items-center gap-1 text-[10px] text-amber-400">
                            <AlertCircle className="h-3 w-3" />
                            {lintWarnings.length} warning{lintWarnings.length > 1 ? 's' : ''}
                        </div>
                    )}
                </div>
                <div
                    ref={editorContainerRef}
                    className="min-h-[280px] h-[280px] overflow-hidden border border-zinc-800 focus-within:border-accent transition-colors rounded-sm"
                />
                {/* Lint warnings */}
                {lintWarnings.length > 0 && (
                    <div className="space-y-1 pt-1">
                        {lintWarnings.map((w, i) => (
                            <div key={i} className={`flex items-start gap-2 text-[10px] font-mono ${w.severity === 'error' ? 'text-red-400' : 'text-amber-400'}`}>
                                <AlertCircle className="h-3 w-3 mt-0.5 shrink-0" />
                                <span>Line {w.line}: {w.message}</span>
                            </div>
                        ))}
                    </div>
                )}
            </div>

            {/* ── Input Schema (JSON) ──────────────────────────────────────── */}
            <div className="space-y-1">
                <label className="text-[10px] uppercase font-bold text-zinc-500">Input Schema (JSON)</label>
                <textarea
                    value={schemaStr}
                    onChange={e => handleSchemaChange(e.target.value)}
                    className="w-full bg-zinc-950 border border-zinc-800 p-3 text-[10px] font-mono text-zinc-300 focus:border-accent focus:outline-none resize-none min-h-[140px] rounded-md"
                    placeholder='{"type": "object", "properties": {"query": {"type": "string", "description": "The input query"}}}'
                />
                {schemaError && (
                    <p className="text-[10px] text-red-400 flex items-center gap-1">
                        <AlertCircle className="h-3 w-3 shrink-0" /> {schemaError}
                    </p>
                )}
            </div>

            {/* ── Test Runner ──────────────────────────────────────────────── */}
            <div className="space-y-2 border-t border-zinc-800 pt-4">
                <div className="flex items-center justify-between">
                    <label className="text-[10px] uppercase font-bold text-zinc-500 flex items-center gap-2">
                        <Terminal className="h-3.5 w-3.5" />
                        Test Run
                    </label>
                    <span className="text-[10px] text-zinc-600">Runs in Docker sandbox</span>
                </div>

                <div className="flex gap-2 items-start">
                    <div className="flex-1 space-y-1">
                        <label className="text-[9px] text-zinc-600 uppercase tracking-wider">Test Arguments (JSON)</label>
                        <textarea
                            value={testArgs}
                            onChange={e => setTestArgs(e.target.value)}
                            rows={2}
                            className="w-full bg-zinc-950 border border-zinc-800 p-2 text-xs font-mono text-zinc-300 focus:border-accent focus:outline-none resize-none rounded-md"
                            placeholder='{"query": "hello world"}'
                        />
                    </div>
                    <button
                        onClick={handleTestRun}
                        disabled={testLoading || !draft.code.trim()}
                        className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover disabled:bg-zinc-800 disabled:text-zinc-600 text-accent-fg text-xs font-bold transition-colors mt-4 shrink-0"
                    >
                        {testLoading
                            ? <><Spinner size={14} /> Running...</>
                            : <><Play className="h-3.5 w-3.5" /> Run</>
                        }
                    </button>
                </div>

                {testError && (
                    <div className="flex items-center gap-2 p-2 bg-red-950/40 border border-red-800 text-red-400 text-[11px] rounded-md">
                        <AlertCircle className="h-4 w-4 shrink-0" />
                        {testError}
                    </div>
                )}

                {testResult && (
                    <div className="space-y-2">
                        {/* Exit code badge */}
                        <div className="flex items-center gap-2">
                            {testResult.exit_code === 0
                                ? <CheckCircle2 className="h-4 w-4 text-green-500" />
                                : <AlertCircle className="h-4 w-4 text-red-500" />
                            }
                            <span className={`text-[11px] font-bold ${testResult.exit_code === 0 ? 'text-green-400' : 'text-red-400'}`}>
                                Exit code: {testResult.exit_code}
                            </span>
                        </div>

                        {testResult.stdout && (
                            <div className="space-y-1">
                                <p className="text-[9px] uppercase tracking-wider text-zinc-500">stdout</p>
                                <pre className="p-3 bg-zinc-950 border border-zinc-800 text-[11px] font-code text-green-300 overflow-auto max-h-48 whitespace-pre-wrap rounded-md">
                                    {testResult.stdout}
                                </pre>
                            </div>
                        )}

                        {testResult.stderr && (
                            <div className="space-y-1">
                                <p className="text-[9px] uppercase tracking-wider text-zinc-500">stderr</p>
                                <pre className="p-3 bg-zinc-950 border border-red-900/40 text-[11px] font-code text-red-300 overflow-auto max-h-32 whitespace-pre-wrap rounded-md">
                                    {testResult.stderr}
                                </pre>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
