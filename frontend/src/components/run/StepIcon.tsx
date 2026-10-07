/*
 * A step type's icon on its role's ground: cyan for a model, amber for a person,
 * grey for logic (D211). The icons are the canvas's (`STEP_TYPE_META.icon`).
 */
import {
    ArrowLeftRight, BookOpenText, Bot, Braces, Code, FileInput, FileText, GitBranch, GitFork, GitMerge,
    RefreshCw, Scale, Square, User, Workflow, Wrench, Zap,
} from 'lucide-react';

import { cn } from '@/lib/cn';
import { STEP_TYPE_META, type StepType } from '@/types/orchestration';

import { roleOf, type StepRole } from './model';

const ICONS: Record<string, React.FC<{ className?: string }>> = {
    Bot, Zap, Wrench, Scale, GitBranch, GitMerge, RefreshCw, User, Code, Braces, GitFork, ArrowLeftRight,
    FileText, Square, BookOpenText, FileInput,
};

const GROUND: Record<StepRole, string> = {
    model: 'bg-machine-subtle text-accent-ink',
    person: 'bg-person-subtle text-person-ink',
    logic: 'bg-surface-2 text-text-muted',
};

/** A step type's own name ("If / Else"), or the type as stored. */
export function typeLabel(type: string): string {
    if (type === 'nested') return 'Nested run';
    return STEP_TYPE_META[type as StepType]?.label ?? type;
}

export function StepIcon({ type, className }: { type: string; className?: string }) {
    const Icon = type === 'nested' ? Workflow : ICONS[STEP_TYPE_META[type as StepType]?.icon ?? ''] ?? Bot;
    return (
        <span aria-hidden className={cn('inline-flex size-6 shrink-0 items-center justify-center rounded-md', GROUND[roleOf(type)], className)}>
            <Icon className="size-3.5" />
        </span>
    );
}
