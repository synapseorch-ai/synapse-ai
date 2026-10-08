/*
 * The run screens (M2b), shared by both products: OSS builds them here, and
 * cloud's `scripts/sync-ui.sh` copies the folder byte for byte. Each product
 * supplies a `RunHost` (`host.tsx`) and maps its API rows onto `model.ts`'s
 * shapes; nothing in this folder reads an API.
 */
export { ApprovalCard } from './ApprovalCard';
export { ComingUp, upcoming, type StartWays, type Upcoming } from './ComingUp';
export { explainFailure, parentRunId, type Explained, type FixKind } from './failures';
export { FailureCard, failureOf } from './FailureCard';
export {
    barHeights, byHandFavourite, dayBox, firstName, greeting, MAX_CARDS, rate, startDone, startSteps, statusLine, today,
    WEEK_BAR, WEEK_MAX, weekRuns, weekStats, type StartFacts, type StartStep, type StartStepId, type WeekStats,
} from './home';
export { GettingStarted, START_SUGGESTIONS, type StartSuggestion } from './GettingStarted';
export { HomeSection } from './HomeSection';
export { HomeStats } from './HomeStats';
export { DEFAULT_RUN_HOST, RunHostProvider, useRunHost, type RunHost, type RunLinks } from './host';
export * from './model';
export { NeedsYou, type ToolCall, type WaitingBrief } from './NeedsYou';
export { NestedRuns } from './NestedRuns';
export { PathGlyph } from './PathGlyph';
export { RawEventLog } from './RawEventLog';
export { RecentRuns } from './RecentRuns';
export { RunActivity } from './RunActivity';
export { RunDetail } from './RunDetail';
export { RUN_DOT, RunList, RUN_FILTERS, StatusLine, type RunFilter, type RunSort } from './RunList';
export {
    appendRunLog, applyRunEvent, emptyRunLog, LogLine, MAX_LOG_LINES, degradedWords, line,
    type LineEntry, type LineTone, type LogEntry, type ProseEntry, type RetrievalEntry, type RunLogState,
    type StepResultEntry, type ToolCallEntry, type ToolResultEntry,
} from './RunLogView';
export { RunOutputs, outputsOf } from './RunOutputs';
export { RunStepper } from './RunStepper';
export { RunTimeline } from './RunTimeline';
export { RunWeek } from './RunWeek';
export { SharedState } from './SharedState';
export { OutputValue, StepDetail, UsageLine, hasDetail, outputOf } from './StepDetail';
export { StepIcon, typeLabel } from './StepIcon';
export { TaskChecklist } from './TaskChecklist';
export { TimeOfDay, Took, useClock, When } from './When';
