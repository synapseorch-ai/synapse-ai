'use client';
/**
 * The workflow editor's full-response view: a step's whole answer, opened from
 * its bubble in the run log. The log itself is the run screens' own
 * (`applyRunEvent` + `LogLine`, M3a-2); this view stays the editor's because it
 * pretty-prints an Extract JSON step's output, which the run page's does not.
 */
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Bot, Check, Copy, X } from 'lucide-react';
import { Markdown } from '@/components/ui';

// The full document treatment — styled headings, lists, tables, code — shared
// by every modal that shows model-written markdown.
export function ResponseModal({ stepName, stepType, content, onClose }: { stepName: string; stepType?: string; content: string; onClose: () => void }) {
    // Try to pretty-print JSON for extract_json steps
    const isJson = stepType === 'extract_json';
    let formattedJson = content;
    let jsonParseOk = false;
    if (isJson) {
        try {
            const parsed = JSON.parse(content);
            formattedJson = JSON.stringify(parsed, null, 2);
            jsonParseOk = true;
        } catch {
            // content may already be pretty or non-JSON — just display as-is
            formattedJson = content;
        }
    }
    const [copied, setCopied] = useState(false);
    useEffect(() => {
        const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
        document.addEventListener('keydown', handler);
        return () => document.removeEventListener('keydown', handler);
    }, [onClose]);

    return createPortal(
        <div
            className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
            onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
        >
            {/* Backdrop */}
            <div className="absolute inset-0 bg-scrim/70 backdrop-blur-sm" />
            {/* Panel */}
            <div className="relative z-10 flex max-h-[80vh] w-full max-w-3xl flex-col rounded-xl border border-border bg-surface shadow-elevated">
                {/* Header */}
                <div className="flex shrink-0 items-center justify-between border-b border-border px-5 py-3">
                    <div className="flex items-center gap-2">
                        <Bot size={14} className="text-machine" aria-hidden />
                        <span className="text-sm font-semibold text-text">{stepName}</span>
                        <span className="text-xs text-text-faint">— full response</span>
                    </div>
                    <div className="flex items-center gap-1">
                        <button
                            onClick={() => { navigator.clipboard.writeText(isJson ? formattedJson : content); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
                            className="rounded-md p-1 text-text-muted transition-colors hover:bg-surface-2 hover:text-text"
                            title="Copy to clipboard"
                        >
                            {copied ? <Check size={15} className="text-success" /> : <Copy size={15} />}
                        </button>
                        <button onClick={onClose} aria-label="Close" className="rounded-md p-1 text-text-muted transition-colors hover:bg-surface-2 hover:text-text">
                            <X size={15} />
                        </button>
                    </div>
                </div>
                {/* Content */}
                <div className="flex-1 overflow-y-auto p-5">
                    {isJson ? (
                        /* Pretty-printed JSON: keys in the link ink, values in the text's (colour is kept for meaning) */
                        <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-lg border border-border bg-surface-2 p-4 font-code text-xs leading-relaxed text-text">
                            {(jsonParseOk ? formattedJson : content)
                                .split('\n')
                                .map((line, i) => {
                                    const coloured = line
                                        .replace(/("(?:[^"\\]|\\.)*")(\s*:)/g, '<span class="text-accent-ink">$1</span><span class="text-text-faint">$2</span>')
                                        .replace(/:\s*("(?:[^"\\]|\\.)*")/g, ': <span class="text-text">$1</span>')
                                        .replace(/:\s*(\d+\.?\d*)/g, ': <span class="text-text-muted">$1</span>')
                                        .replace(/:\s*(true|false|null)\b/g, ': <span class="text-text-muted">$1</span>');
                                    return (
                                        <span key={i} dangerouslySetInnerHTML={{ __html: coloured + '\n' }} />
                                    );
                                })}
                        </pre>
                    ) : (
                        <Markdown className="max-w-none">{content}</Markdown>
                    )}
                </div>
            </div>
        </div>,
        document.body
    );
}
