/* eslint-disable @typescript-eslint/no-explicit-any */
'use client';

/**
 * A Python tool: its code, its input schema, and a test run.
 *
 * The three editors are the kit's `CodeEditor`. This file used to build its own
 * CodeMirror with the change listener bound once at mount, so it called
 * `onChange({...draft, code})` with the draft *as it was then*: a name or a
 * description typed after the editor opened was wiped by the next keystroke in
 * the code (2026-09-14). `CodeEditor` reads `onChange` fresh on every change.
 */

import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';
import { Play, ChevronDown, ChevronUp, Terminal, Package, AlertCircle, CheckCircle2 } from 'lucide-react';

import { CodeEditor, Combobox, Spinner, Tag } from '@/components/ui';
/* ─── Types ──────────────────────────────────────────────────────────────── */

export interface PythonDraftTool {
    name: string;
    generalName: string;
    description: string;
    tool_type: 'python';
    code: string;
    inputSchema: any;
    schemaParams?: any[];
    /**
     * Workspace variables this tool may read, handed to the code as `_globals`.
     * The only way a Python tool receives one: arguments the model writes are
     * never filled in with variables, because a prompt-injected page could name
     * any key (F28, batch 1A). A name declared but not set arrives absent.
     */
    globals?: string[];
}

interface PythonToolEditorProps {
    draft: PythonDraftTool;
    onChange: (updated: PythonDraftTool) => void;
}

/* ─── Sandbox packages info ───────────────────────────────────────────────── */

const PACKAGES = [
    'pandas', 'numpy', 'scipy', 'scikit-learn', 'matplotlib',
    'seaborn', 'requests', 'httpx', 'beautifulsoup4', 'lxml',
    'openpyxl', 'xlsxwriter', 'pyyaml', 'tabulate', 'jinja2',
    'jsonschema', 'pillow', 'sympy',
];

const DEFAULT_CODE = `# _args contains all tool arguments as a Python dict
# _globals holds the workspace variables this tool declares (below the schema)
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
    const [showPackages, setShowPackages] = useState(false);
    const [testArgs, setTestArgs] = useState('{}');
    const [testResult, setTestResult] = useState<{
        stdout: string; stderr: string; exit_code: number;
    } | null>(null);
    const [testLoading, setTestLoading] = useState(false);
    const [testError, setTestError] = useState<string | null>(null);
    // Names only. The route returns values too; this editor never holds them.
    const [variableNames, setVariableNames] = useState<string[]>([]);
    useEffect(() => {
        let cancelled = false;
        fetch('/api/settings/variables')
            .then(r => (r.ok ? r.json() : { values: {} }))
            .then(d => { if (!cancelled) setVariableNames(Object.keys(d?.values ?? {}).sort()); })
            .catch(() => undefined);
        return () => { cancelled = true; };
    }, []);
    const declared = draft.globals ?? [];
    const declare = (name: string) =>
        onChange({ ...draft, globals: [...declared.filter(n => n !== name), name] });
    const undeclare = (name: string) =>
        onChange({ ...draft, globals: declared.filter(n => n !== name) });
    // Exactly the draft: a fallback here would put the starter code back the moment
    // someone cleared the editor. Switching to Python seeds it instead.
    const code = draft.code ?? '';
    const lintWarnings = useMemo(() => lintPython(code), [code]);

    /* ── Schema JSON state ───────────────────────────────────────────────── */

    const [schemaStr, setSchemaStr] = useState(() =>
        JSON.stringify(draft.inputSchema || { type: 'object', properties: { input: { type: 'string' } } }, null, 2)
    );
    // The editor says when the JSON does not parse; the draft keeps the last schema that did.
    const handleSchemaChange = (val: string) => {
        setSchemaStr(val);
        try {
            onChange({ ...draft, inputSchema: JSON.parse(val) });
        } catch {
            /* mid-edit */
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
                body: JSON.stringify({ code: draft.code, args, timeout: 30, globals: declared }),
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
                        className="text-[10px] text-text-faint hover:text-zinc-300 flex items-center gap-1"
                    >
                        {showPackages ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                        {showPackages ? 'Hide' : 'Show'} packages
                    </button>
                </div>
                <p className="text-[11px] text-text-faint leading-relaxed">
                    Code runs in a <span className="text-accent font-mono">Docker sandbox</span> (512MB RAM, 1 CPU, internet enabled).
                    Vault files are mounted at <span className="font-mono text-accent">/data/</span>.
                    Arguments are injected as <span className="font-mono text-accent">_args</span> (dict),
                    and the variables it declares as <span className="font-mono text-accent">_globals</span>.
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
                    <label className="text-[10px] uppercase font-bold text-text-faint">Python Code</label>
                    {lintWarnings.length > 0 && (
                        <div className="flex items-center gap-1 text-[10px] text-amber-400">
                            <AlertCircle className="h-3 w-3" />
                            {lintWarnings.length} warning{lintWarnings.length > 1 ? 's' : ''}
                        </div>
                    )}
                </div>
                <CodeEditor
                    language="python"
                    height={280}
                    value={code}
                    onChange={(next) => onChange({ ...draft, code: next })}
                    placeholder={DEFAULT_CODE}
                    aria-label="Python code"
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
                <label className="text-[10px] uppercase font-bold text-text-faint">Input Schema (JSON)</label>
                <CodeEditor
                    language="json"
                    lint="json"
                    format
                    height={200}
                    value={schemaStr}
                    onChange={handleSchemaChange}
                    placeholder='{"type": "object", "properties": {"query": {"type": "string", "description": "The input query"}}}'
                    aria-label="Input schema (JSON)"
                />
            </div>

            {/* ── Declared globals ─────────────────────────────────────────── */}
            <div className="space-y-2">
                <p id="py-globals-label" className="text-[10px] uppercase font-bold text-text-faint">
                    Variables this tool can read
                </p>
                <p className="text-[11px] text-text-faint leading-relaxed">
                    Each arrives in <span className="font-mono text-accent">_globals</span>, as{' '}
                    <span className="font-mono">_globals[&quot;NAME&quot;]</span>. Nothing else does: arguments
                    the agent writes are never filled in with variables. One declared but not set is simply absent.
                </p>
                {declared.length > 0 && (
                    <div className="flex flex-wrap gap-1.5" aria-labelledby="py-globals-label">
                        {declared.map(name => (
                            <Tag key={name} onRemove={() => undeclare(name)}>
                                {variableNames.length && !variableNames.includes(name) ? `${name} (not set)` : name}
                            </Tag>
                        ))}
                    </div>
                )}
                {variableNames.some(n => !declared.includes(n)) ? (
                    <Combobox
                        value={undefined}
                        onChange={declare}
                        options={variableNames.filter(n => !declared.includes(n)).map(n => ({ value: n, label: n }))}
                        placeholder="Add a variable…"
                        searchPlaceholder="Search variables…"
                        aria-label="Add a variable this tool can read"
                        size="sm"
                        className="max-w-xs"
                    />
                ) : (
                    <p className="text-[11px] text-text-faint">
                        {variableNames.length ? 'Every workspace variable is declared. ' : 'No workspace variables yet. '}
                        <Link href="/settings/variables" className="text-accent hover:underline">Settings › Variables</Link>
                    </p>
                )}
            </div>

            {/* ── Test Runner ──────────────────────────────────────────────── */}
            <div className="space-y-2 border-t border-zinc-800 pt-4">
                <div className="flex items-center justify-between">
                    <label className="text-[10px] uppercase font-bold text-text-faint flex items-center gap-2">
                        <Terminal className="h-3.5 w-3.5" />
                        Test Run
                    </label>
                    <span className="text-[10px] text-text-faint">Runs in Docker sandbox</span>
                </div>

                <div className="flex gap-2 items-start">
                    <div className="flex-1 space-y-1">
                        <label className="text-[9px] text-text-faint uppercase tracking-wider">Test Arguments (JSON)</label>
                        <CodeEditor
                            language="json"
                            height={88}
                            value={testArgs}
                            onChange={setTestArgs}
                            placeholder='{"query": "hello world"}'
                            aria-label="Test arguments (JSON)"
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
                                <p className="text-[9px] uppercase tracking-wider text-text-faint">stdout</p>
                                <pre className="p-3 bg-zinc-950 border border-zinc-800 text-[11px] font-code text-green-300 overflow-auto max-h-48 whitespace-pre-wrap rounded-md">
                                    {testResult.stdout}
                                </pre>
                            </div>
                        )}

                        {testResult.stderr && (
                            <div className="space-y-1">
                                <p className="text-[9px] uppercase tracking-wider text-text-faint">stderr</p>
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
