import type { StepIssue } from '@/types/orchestration';

/** Where a run stands, as the editor tracks it. */
export type WorkbenchRunPhase = 'idle' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';

/** One of "N to check": a step's issue (it opens the step) or the workflow's (it opens Workflow settings). */
export type WorkbenchIssue = StepIssue & { stepId: string | null; stepName?: string };

/** An entry in the bar's ⋯ menu, after Workflow settings. */
export type WorkbenchMenuItem = {
    id: string;
    label: string;
    icon?: React.ComponentType<{ className?: string }>;
    onSelect: () => void;
    danger?: boolean;
    disabled?: boolean;
    /** A rule above it. */
    separatorBefore?: boolean;
};
