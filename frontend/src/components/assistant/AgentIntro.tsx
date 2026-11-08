/*
 * Who a new conversation is with (M2c, Naveen's call): the agent's name, its
 * model and whether it is ready, what it is for, and what it can use — its
 * knowledge and its tools. Prototype 2's chat opens on the agent ("Order desk ·
 * Nova Lite · answers from Compliance guides"), not on a tour of the product.
 *
 * Presentational: each product passes the agent as it reads it.
 */
import { Bot, BookOpen, Wrench } from 'lucide-react';
import type * as React from 'react';

import { humanizeToolName } from '@/lib/toolNames';

/** How many tools to name before "and N more". */
const TOOLS_SHOWN = 8;

export function AgentIntro({
    name, model, status, description, tools = [], knowledge = [], icon,
}: {
    name: string;
    /** The model it answers with, or "Workflow · 3 steps" for a workflow agent. */
    model?: React.ReactNode;
    /** Its state beside the model: "Ready", "Model not ready"… */
    status?: React.ReactNode;
    description?: string | null;
    /** Tool ids, named the way the rest of the product names them. */
    tools?: readonly string[];
    /** Knowledge bases, by name. */
    knowledge?: readonly string[];
    icon?: React.ReactNode;
}) {
    const shown = tools.slice(0, TOOLS_SHOWN);
    const rest = tools.length - shown.length;
    return (
        <section aria-label="Who you are talking to" className="flex w-full max-w-xl flex-col items-center gap-3 text-center">
            <span aria-hidden className="flex size-12 items-center justify-center rounded-2xl bg-machine-subtle text-accent-ink">
                {icon ?? <Bot className="size-6" />}
            </span>
            <div className="flex w-full min-w-0 flex-col items-center gap-1">
                {/* One line, the whole name on hover: a long name ran off the page (F217). */}
                <h2 data-agent-label className="max-w-full truncate text-2xl font-semibold tracking-tight text-text" title={name}>
                    {name}
                </h2>
                {(model || status) && (
                    <p className="flex max-w-full flex-wrap items-center justify-center gap-x-1.5 text-[13px] text-text-muted">
                        {model && <span className="truncate">{model}</span>}
                        {model && status && <span aria-hidden>·</span>}
                        {status}
                    </p>
                )}
            </div>
            {description && <p className="max-w-[52ch] text-[15px] leading-relaxed text-text-muted">{description}</p>}
            {(knowledge.length > 0 || tools.length > 0) && (
                <div className="flex max-w-full flex-col items-center gap-1.5">
                    <span className="text-xs font-medium text-text-faint">Can use</span>
                    <ul className="flex flex-wrap justify-center gap-1.5">
                        {knowledge.map((kb) => (
                            <li key={`kb-${kb}`} className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-text">
                                <BookOpen className="size-3.5 shrink-0 text-machine" aria-hidden />
                                <span className="truncate">{kb}</span>
                            </li>
                        ))}
                        {shown.map((tool) => (
                            <li key={`tool-${tool}`} className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-text-muted">
                                <Wrench className="size-3.5 shrink-0 text-machine" aria-hidden />
                                <span className="truncate" title={tool}>{humanizeToolName(tool)}</span>
                            </li>
                        ))}
                        {rest > 0 && (
                            <li className="inline-flex items-center rounded-full px-2 py-1 text-xs text-text-faint">and {rest} more</li>
                        )}
                    </ul>
                </div>
            )}
        </section>
    );
}
