"use client";

/**
 * The agent's working plan, as a checklist.
 *
 * The engine's native task list (`core/task_list.py`) is whole-list-write: the
 * model submits every task with its current status on each call, and the engine
 * checkpoints the result onto the run. So there is exactly one list per run at
 * any moment, and both places that show it are showing the same object — the
 * builder's run panel as `tasks_updated` events arrive, and the run page from
 * `orchestration_runs.tasks` after the fact.
 *
 * One component for both, on the semantic tokens. Moved upstream from cloud's
 * `components/TaskChecklist.tsx` with the run screens (M2b), so the run page's
 * plan is the same object in both products.
 */

import { ChevronRight, Circle, CircleCheck, CircleDot, CircleX, ListTodo } from "lucide-react";
import * as React from "react";

import { cn, focusRing } from "@/lib/cn";

import type { Task, TaskStatus } from "./model";

export type { Task, TaskStatus };

const MARK: Record<TaskStatus, { icon: typeof Circle; cls: string }> = {
  completed: { icon: CircleCheck, cls: "text-success" },
  in_progress: { icon: CircleDot, cls: "text-accent-ink" },
  cancelled: { icon: CircleX, cls: "text-text-faint" },
  pending: { icon: Circle, cls: "text-text-faint" },
};

const TEXT: Record<TaskStatus, string> = {
  // Struck through, not hidden. A finished plan read back later is the record
  // of what the agent decided to do, and a completed task is part of that.
  completed: "text-text-faint line-through",
  in_progress: "text-text",
  cancelled: "text-text-faint line-through",
  pending: "text-text-muted",
};

export function TaskChecklist({
  tasks,
  stepName,
  showHeader = true,
  collapsible = false,
  defaultOpen = true,
  className,
}: {
  tasks: Task[];
  /** Which step last wrote the list, when that is known. */
  stepName?: string;
  /**
   * Turn the header into a toggle.
   *
   * On where the list shares a scroll with something else you are reading — a
   * chat transcript, where a six-task plan pushes the answer off the screen
   * every time the agent revises it. Off where the list is the point of its own
   * container.
   */
  collapsible?: boolean;
  defaultOpen?: boolean;
  /**
   * The "TASKS n/m done" line. Off where the container already says it — the
   * run panel's pill carries the count in its own label, and repeating it
   * directly underneath is the same fact twice in twelve pixels.
   */
  showHeader?: boolean;
  className?: string;
}) {
  const [open, setOpen] = React.useState(defaultOpen);

  if (tasks.length === 0) return null;
  const done = tasks.filter((t) => t.status === "completed").length;
  const shown = collapsible ? open : true;

  const heading = (
    <>
      {collapsible && (
        <ChevronRight
          className={cn("size-3 shrink-0 text-text-faint transition-transform", open && "rotate-90")}
          aria-hidden
        />
      )}
      <ListTodo className="size-3 shrink-0 text-accent-ink" aria-hidden />
      <span className="text-2xs font-semibold uppercase tracking-wider text-text-muted">Tasks</span>
      <span className="text-2xs text-text-faint">
        {done}/{tasks.length} done
        {stepName ? ` · ${stepName}` : ""}
      </span>
    </>
  );

  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-surface-2/40 px-3 py-2 font-sans",
        className,
      )}
    >
      {showHeader &&
        (collapsible ? (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className={cn(
              "-mx-1 mb-1 flex w-[calc(100%+0.5rem)] items-center gap-1.5 rounded px-1 py-0.5 text-left transition-colors hover:bg-surface-2/60",
              focusRing,
            )}
          >
            {heading}
          </button>
        ) : (
          <div className="mb-1 flex items-center gap-1.5">{heading}</div>
        ))}
      {shown && (
      <ul className="space-y-0.5">
        {tasks.map((task, i) => {
          const mark = MARK[task.status] ?? MARK.pending;
          const Icon = mark.icon;
          return (
            <li key={i} className="flex items-start gap-1.5 text-xs leading-snug">
              <Icon className={cn("mt-[2px] size-3 shrink-0", mark.cls)} aria-hidden />
              <span className={TEXT[task.status] ?? TEXT.pending}>{task.content}</span>
            </li>
          );
        })}
      </ul>
      )}
    </div>
  );
}
