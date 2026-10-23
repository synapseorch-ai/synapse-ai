'use client';
/**
 * A step's result, from its button on the canvas (F902). Naveen: "on the running
 * workflow lets have some icon or something in a step to see its results by
 * clicking on something in the step itself". It opens in the floating card a
 * step's setup uses (TABS1's approved mockup draws it there): what the step
 * wrote, why it failed, what it spent, in this run, and the way to the whole run.
 */
import { X } from 'lucide-react';

import { IconButton, LinkButton } from '@/components/ui';
import type { StepConfig } from '@/types/orchestration';

import { StepIcon } from '../../run/StepIcon';
import { StepDetail } from '../../run/StepDetail';
import { pillText, type StepExecution, type StepTime, type StepUsage } from '../../run/model';

export function StepResultCard({ step, result, time, sharedState, runHref, onClose }: {
    step: StepConfig;
    result: { step: StepExecution; usage?: StepUsage };
    /** The step's line from Last run: how many times, how long, what it spent. */
    time?: StepTime | null;
    sharedState: Record<string, unknown>;
    runHref?: string | null;
    onClose: () => void;
}) {
    const said = time ? pillText(time).full : '';
    return (
        <aside className="relative flex h-full min-h-0 w-full flex-col" aria-label={`${step.name}: result`}>
            <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
                <StepIcon type={step.type} className="size-7 [&_svg]:size-4" />
                <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold tracking-[-0.01em] text-text">
                    {step.name} <span className="font-normal text-text-faint">· result</span>
                </h3>
                <IconButton label="Close the result" onClick={onClose} size="sm" icon={X} />
            </div>
            <div className="flex-1 space-y-3 overflow-y-auto p-4">
                <p className="text-xs text-text-faint">{said ? `${said} · this run` : 'This run'}</p>
                <StepDetail step={result.step} sharedState={sharedState} usage={result.usage} />
            </div>
            {runHref && (
                <div className="border-t border-border px-4 py-3">
                    <LinkButton href={runHref} variant="secondary" size="sm">Open the run</LinkButton>
                </div>
            )}
        </aside>
    );
}
