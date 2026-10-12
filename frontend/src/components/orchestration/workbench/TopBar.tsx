'use client';
/**
 * The open canvas's floating bar (Prototype 4, D204): back, the name, the save
 * state on the left; what to check, Change with AI, Run now or Stop, help and
 * ⋯ on the right. Two floating groups over the canvas, not a header.
 */
import { useState } from 'react';
import * as RPopover from '@radix-ui/react-popover';
import { AlertTriangle, ArrowLeft, ChevronDown, Clock, MoreHorizontal, Play, Save, SlidersHorizontal, Square } from 'lucide-react';
import {
    Button, Hint, Mark, Menu, MenuItem, MenuLabel, MenuSeparator, PageHeaderSlot, Spinner, Textarea, buttonStyles,
} from '@/components/ui';
import { cn } from '@/lib/cn';
import type { WorkbenchIssue, WorkbenchMenuItem, WorkbenchRunPhase } from './types';

/** One floating group: the surface, a hairline and the shadow, as every piece of chrome on the canvas. */
export const FLOAT = 'rounded-[10px] border border-border bg-surface shadow-elevated';

export function TopBar({
    name, onRename, onBack, readOnly = false, switcher,
    dirty, saving, onSave,
    issues, onIssue,
    onChangeWithAI,
    phase, onRun, onStop, blocked, runInput, lastRun,
    menu, onOpenSettings, settingsOpen,
    phone, compact, insetRight,
}: {
    name: string;
    onRename: (name: string) => void;
    onBack: () => void;
    /** A viewer: the name is text, and nothing that saves, runs or changes the workflow shows. */
    readOnly?: boolean;
    switcher?: React.ReactNode;
    dirty: boolean;
    saving: boolean;
    onSave: () => void;
    issues: WorkbenchIssue[];
    onIssue: (issue: WorkbenchIssue) => void;
    onChangeWithAI?: () => void;
    phase: WorkbenchRunPhase;
    onRun: () => void;
    onStop: () => void;
    /** Errors that stop a run: Run now looks disabled and says how many. */
    blocked: number;
    /** The run's input, behind Run now's chevron. Run now sends it as it stands, empty or not. */
    runInput?: { value: string; onChange: (value: string) => void; note?: string; estimate?: React.ReactNode };
    /** Last run: show each step's time from the last run on the steps (M3a-2). */
    lastRun?: { shown: boolean; onShow: (shown: boolean) => void; loading?: boolean };
    menu: WorkbenchMenuItem[];
    onOpenSettings: () => void;
    settingsOpen: boolean;
    phone: boolean;
    /** Less room: labels give way to icons. */
    compact: boolean;
    insetRight: number;
}) {
    const live = phase === 'running' || phase === 'paused';
    const errors = issues.filter((i) => i.severity === 'error');
    const warnings = issues.filter((i) => i.severity === 'warning');
    const ordered = [...errors, ...warnings];

    return (
        <div
            className="nokey pointer-events-none absolute left-2 top-2 z-10 flex items-start justify-between gap-2 md:left-3 md:top-3"
            style={{ right: (phone ? 8 : 12) + insetRight }}
        >
            {/* Left: back, the name (and the switcher), the save state */}
            <div data-canvas-chrome="bar" className={cn(FLOAT, 'pointer-events-auto flex h-10 min-w-0 items-center gap-0.5 p-1 md:h-[42px]')}>
                <button
                    type="button"
                    onClick={onBack}
                    aria-label="Back"
                    title="Back to all orchestrations and runs"
                    className={buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: true })}
                >
                    <ArrowLeft className="size-4" aria-hidden />
                </button>
                <span className="mx-1 h-5 w-px shrink-0 bg-border" aria-hidden />
                {/* Always a field, as it was: type to rename, and ⌘S inside it saves the name.
                    A viewer reads it as text. */}
                {readOnly ? (
                    <span className="max-w-[min(300px,34vw)] truncate px-2 text-sm font-semibold tracking-[-0.01em] text-text" translate="no">
                        {name || 'Untitled workflow'}
                    </span>
                ) : (
                <input
                    aria-label="Orchestration name"
                    placeholder="Orchestration name"
                    value={name}
                    onChange={(e) => onRename(e.target.value)}
                    spellCheck={false}
                    className={cn(
                        'field-sizing-content h-8 min-w-[6ch] rounded-md border border-transparent bg-transparent px-2 text-sm font-semibold tracking-[-0.01em] text-text outline-none',
                        'max-w-[min(300px,34vw)] truncate hover:border-border focus:border-accent',
                    )}
                    data-no-focus-ring
                />
                )}
                {switcher}
                {!readOnly && <>
                <span className="mx-1 h-5 w-px shrink-0 bg-border" aria-hidden />
                {!phone && (
                    <span className="hidden whitespace-nowrap px-1 text-xs text-text-faint lg:inline" aria-live="polite">
                        {dirty ? 'Unsaved changes' : 'Saved'}
                    </span>
                )}
                <Button
                    size="sm"
                    variant={dirty ? 'secondary' : 'ghost'}
                    onClick={onSave}
                    disabled={saving}
                    title={dirty ? 'Unsaved changes (Ctrl+S)' : 'Saved (Ctrl+S)'}
                    iconOnly={phone}
                    aria-label={phone ? 'Save' : undefined}
                >
                    {saving ? <Spinner size={16} /> : <Save className="size-3.5" aria-hidden />}
                    {!phone && 'Save'}
                    {dirty && <span className="size-1.5 rounded-full bg-accent" aria-label="Unsaved changes" />}
                </Button>
                </>}
            </div>

            {/* Right: what to check, Change with AI, Run now or Stop, help, ⋯ */}
            <div data-canvas-chrome="bar" className={cn(FLOAT, 'pointer-events-auto flex h-10 shrink-0 items-center gap-1 p-1 md:h-[42px]')}>
                {ordered.length > 0 && !live && !readOnly && (
                    <Menu
                        align="end"
                        trigger={
                            <button
                                type="button"
                                aria-label={phone ? `${ordered.length} to check` : undefined}
                                className={cn(buttonStyles({ variant: 'ghost', size: 'sm' }), 'gap-1.5 font-medium text-text')}
                            >
                                <span className="grid size-[18px] place-items-center rounded-full bg-text text-[11px] font-bold leading-none text-bg" aria-hidden>
                                    !
                                </span>
                                {!phone && <span>{ordered.length} to check</span>}
                            </button>
                        }
                    >
                        <MenuLabel>
                            {ordered.length === 1 ? 'One thing to check' : `${ordered.length} things to check`}
                            {errors.length > 0 ? ' before it can run' : ''}
                        </MenuLabel>
                        {ordered.map((issue, i) => (
                            <MenuItem key={i} icon={AlertTriangle} onSelect={() => onIssue(issue)}>
                                <span className="min-w-0">
                                    <span className="block truncate text-text">
                                        {issue.stepName ? `${issue.stepName}: ` : ''}{issue.message}
                                    </span>
                                    <span className="block text-2xs text-text-faint">
                                        {issue.severity === 'error' ? 'Error' : 'Warning'}
                                        {issue.stepId ? ' · open the step' : ' · open workflow settings'}
                                    </span>
                                </span>
                            </MenuItem>
                        ))}
                    </Menu>
                )}
                {!phone && (
                    <button
                        type="button"
                        onClick={onOpenSettings}
                        aria-pressed={settingsOpen}
                        aria-label={compact ? 'Workflow settings' : undefined}
                        title="Workflow settings"
                        className={cn(
                            buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: compact }),
                            settingsOpen && 'bg-surface-2 text-text',
                        )}
                    >
                        <SlidersHorizontal className="size-3.5" aria-hidden />
                        {!compact && 'Workflow settings'}
                    </button>
                )}
                {lastRun && !phone && (
                    <button
                        type="button"
                        onClick={() => lastRun.onShow(!lastRun.shown)}
                        aria-pressed={lastRun.shown}
                        aria-busy={lastRun.loading || undefined}
                        aria-label={compact ? 'Last run' : undefined}
                        title="Show each step's time from the last run"
                        className={cn(
                            buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: compact }),
                            lastRun.shown && 'bg-surface-2 text-text',
                        )}
                    >
                        {lastRun.loading ? <Spinner size={14} /> : <Clock className="size-3.5" aria-hidden />}
                        {!compact && 'Last run'}
                    </button>
                )}
                {onChangeWithAI && !phone && !readOnly && (
                    <Button
                        size="sm"
                        variant="ghost"
                        onClick={onChangeWithAI}
                        aria-label={compact ? 'Change with AI' : undefined}
                        title={compact ? 'Change with AI' : undefined}
                        className="text-text"
                    >
                        <Mark size={20} compact={false} className="-my-0.5 text-accent" />
                        {!compact && 'Change with AI'}
                    </Button>
                )}
                {readOnly ? null : live ? (
                    <Button size="sm" variant="secondary" onClick={onStop}>
                        <Square className="size-3.5" aria-hidden /> Stop
                    </Button>
                ) : (
                    <RunNow
                        onRun={onRun}
                        blocked={blocked}
                        phone={phone}
                        runInput={runInput}
                    />
                )}
                <PageHeaderSlot />
                <Menu
                    align="end"
                    trigger={
                        <button
                            type="button"
                            aria-label="More actions"
                            title="More actions"
                            className={buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: true })}
                        >
                            <MoreHorizontal className="size-4" aria-hidden />
                        </button>
                    }
                >
                    <MenuItem icon={SlidersHorizontal} onSelect={onOpenSettings}>Workflow settings</MenuItem>
                    {phone && onChangeWithAI && !readOnly && <MenuItem icon={MarkIcon} onSelect={onChangeWithAI}>Change with AI</MenuItem>}
                    {phone && lastRun && (
                        <MenuItem icon={Clock} onSelect={() => lastRun.onShow(!lastRun.shown)}>
                            {lastRun.shown ? 'Hide last run' : 'Show last run'}
                        </MenuItem>
                    )}
                    {menu.map((item) => (
                        <MenuEntry key={item.id} item={item} />
                    ))}
                </Menu>
            </div>
        </div>
    );
}

function MenuEntry({ item }: { item: WorkbenchMenuItem }) {
    return (
        <>
            {item.separatorBefore && <MenuSeparator />}
            <MenuItem icon={item.icon} tone={item.danger ? 'danger' : 'default'} disabled={item.disabled} onSelect={item.onSelect}>
                {item.label}
            </MenuItem>
        </>
    );
}

/** The mark at a menu icon's size; below 20 px it takes its two-dot form, as everywhere (D213). */
function MarkIcon({ className }: { className?: string }) {
    return <Mark size={16} className={cn('text-accent', className)} />;
}

/**
 * Run now, split (Naveen, M3a-1 review): the button runs at once with the input
 * as it stands — none is fine — and the chevron beside it opens the input, with
 * a Run of its own. The input sat under the old canvas, on the Run Log tab.
 */
function RunNow({ onRun, blocked, phone, runInput }: {
    onRun: () => void;
    blocked: number;
    phone: boolean;
    runInput?: { value: string; onChange: (value: string) => void; note?: string; estimate?: React.ReactNode };
}) {
    const [open, setOpen] = useState(false);
    const text = runInput?.value.trim() ?? '';
    const hint = blocked > 0
        ? `Fix ${blocked} problem${blocked === 1 ? '' : 's'} first`
        : text ? `Runs with “${text.length > 60 ? `${text.slice(0, 60)}…` : text}”` : null;
    const run = () => {
        setOpen(false);
        onRun();
    };
    return (
        // Stretched, so the chevron takes Run now's height: the kit's icon size is 2px shorter.
        <div className="flex items-stretch">
            <Hint content={hint}>
                <Button
                    size="sm"
                    variant="primary"
                    onClick={onRun}
                    aria-disabled={blocked > 0 || undefined}
                    // One name at every width: a phone shows the shorter word.
                    aria-label={phone ? 'Run now' : undefined}
                    className={cn(runInput && 'rounded-r-none', blocked > 0 && 'opacity-50')}
                >
                    <Play className="size-3.5" aria-hidden /> {phone ? 'Run' : 'Run now'}
                </Button>
            </Hint>
            {runInput && (
                <RPopover.Root open={open} onOpenChange={setOpen}>
                    <RPopover.Trigger asChild>
                        <button
                            type="button"
                            aria-label="Run with input"
                            title="Run with input"
                            className={cn(
                                buttonStyles({ variant: 'primary', size: 'sm', iconOnly: true }),
                                'h-auto w-6 rounded-l-none border-l border-accent-fg/25',
                                blocked > 0 && 'opacity-50',
                            )}
                        >
                            <ChevronDown className="size-3.5" aria-hidden />
                        </button>
                    </RPopover.Trigger>
                    <RPopover.Portal>
                        <RPopover.Content
                            align="end"
                            sideOffset={8}
                            collisionPadding={8}
                            className="nokey z-50 w-96 max-w-[calc(100vw-16px)] rounded-lg border border-border-strong bg-surface p-3 shadow-elevated"
                        >
                            <label htmlFor="workbench-run-input" className="mb-1.5 block text-sm font-medium text-text">
                                Input for this run
                            </label>
                            {/* Grows with its lines (Naveen, M3a-1 review), to about eight, then
                                scrolls. Enter runs, as the old input did; Shift+Enter is a new line. */}
                            <Textarea
                                id="workbench-run-input"
                                aria-label="Initial input for this run"
                                size="sm"
                                rows={2}
                                autoFocus
                                value={runInput.value}
                                onChange={(e) => runInput.onChange(e.target.value)}
                                placeholder="Initial input for the orchestration..."
                                className="field-sizing-content max-h-48 min-h-[3.25rem] resize-none"
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                                        e.preventDefault();
                                        run();
                                    }
                                }}
                            />
                            {runInput.note && (
                                <p className="mt-2 text-xs leading-relaxed text-text-muted">{runInput.note}</p>
                            )}
                            {runInput.estimate && <div className="mt-2">{runInput.estimate}</div>}
                            <div className="mt-2.5 flex items-center justify-between gap-3">
                                <span className="text-xs text-text-faint">
                                    <kbd className="rounded border border-border px-1 font-mono text-2xs">Enter</kbd> to run,{' '}
                                    <kbd className="rounded border border-border px-1 font-mono text-2xs">Shift Enter</kbd> for a new line
                                </span>
                                <Button size="sm" variant="primary" onClick={run} aria-disabled={blocked > 0 || undefined}>
                                    <Play className="size-3.5" aria-hidden /> Run
                                </Button>
                            </div>
                        </RPopover.Content>
                    </RPopover.Portal>
                </RPopover.Root>
            )}
        </div>
    );
}
