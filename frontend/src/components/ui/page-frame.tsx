/*
 * The page frame: one header in one place on every screen (UX1, M1a).
 *
 * UX1 measured page titles at seven different x positions in cloud and six in
 * OSS — 288 on a full-width screen, 376 on one centred in `max-w-5xl`, 512 in
 * Settings — and 20px everywhere. A frame that centres its column moves the
 * title whenever the content's width changes; this one never centres. The
 * header and the body start at the same padding on every screen, and the
 * width variants only cap how far the column may run to the right. So the
 * title sits at one x at every viewport, and — because the eyebrow line above
 * it is always drawn, empty or not — at one y. A wide page fills the sheet, and
 * the gutter grows past a 1700 px window (F897), so no width leaves a band empty.
 *
 * It takes words, not a nav entry, for the reason `Screen` always has: a
 * `nav: NavEntry` prop would drag one product's navigation model into a design
 * system the other product also consumes. Each product unpacks its own nav at
 * the call site — and passes its own tab bar (`HubTabs`) as `tabs`.
 *
 * No "use client" — it renders no hooks, so it works from a server page and
 * from inside a client tree alike.
 */
import { cn } from "@/lib/cn";

import { CanvasModeHide } from "./canvas-mode";
import { PageHeader } from "./text";

/**
 * How far the content column may run.
 *
 * `form` is the default because most screens are a column of prose and controls,
 * and a form line longer than about 64rem stops scanning as a line. `wide` is
 * for screens whose content is genuinely tabular: a six-column row needs the
 * width, and capping it puts the row's name at the far left and the action it
 * applies to 160mm away on the right.
 */
export type PageWidth = "form" | "wide";

export const FRAME_WIDTHS: Record<PageWidth | "full", string> = {
  form: "max-w-5xl",
  // Fills the sheet (F897, Naveen's rule B, 2026-10-24): it was 88rem, and above ~1700 px the slack all lay on the
  // right (903 px of it at 2560). A wide page's content is lists, tables and card grids, which use the room.
  wide: "",
  full: "",
};
const WIDTHS = FRAME_WIDTHS;

/**
 * The one horizontal padding, shared by the header and the body so their left edges are the same line. 40 px from
 * `md` up to a 1700 px window, so nothing at 1440 or below moves; then it grows with the screen, to 128 px at most
 * (F897): a page on a big screen keeps margins in proportion instead of an empty band on one side.
 */
export const PAGE_GUTTER = "px-4 md:px-[clamp(2.5rem,calc(4vw_-_1.75rem),8rem)]";
const GUTTER = PAGE_GUTTER;

type FrameHeaderProps = {
  title: React.ReactNode;
  /**
   * One line, shown under the title. Never a template.
   *
   * A node rather than a string because a detail page's subtitle is often the
   * thing being detailed — a run id in `Mono`, not a sentence.
   */
  description?: React.ReactNode;
  /** Right-hand side of the header — a button, or a close control. */
  actions?: React.ReactNode;
  /** The small line above the title: where this page lives ("Build", "Settings"). */
  eyebrow?: React.ReactNode;
  /** A back link on a detail page. It takes the eyebrow's line, so the title does not move. */
  breadcrumb?: React.ReactNode;
  /** A tab bar (`HubTabs`) under the title, standing on the header's rule. */
  tabs?: React.ReactNode;
  bleed?: boolean;
  /** `full` is the header's alone: over a page with an aside it spans the column and the body together. */
  width?: PageWidth | "full";
};

/**
 * The frame's header band on its own, for a screen that lays out its own body
 * — chat, which has a drawer and a composer of its own — but still owes the
 * title its place.
 */
export function PageFrameHeader({
  title, description, actions, eyebrow, breadcrumb, tabs, bleed = false, width = "form",
}: FrameHeaderProps) {
  return (
    // A rule only where something stands on it or runs up to it: a tab bar, or a
    // full-bleed body (a canvas, a log). Elsewhere the space is the divider.
    <header className={cn("shrink-0 font-sans", (tabs || bleed) && "border-b border-border")}>
      <div className={cn("w-full pt-5 md:pt-8", GUTTER, tabs ? "pb-0" : bleed ? "pb-4" : "pb-0", !bleed && WIDTHS[width])}>
        <PageHeader
          eyebrow={breadcrumb ?? eyebrow ?? null}
          title={title}
          description={description}
          actions={actions}
        />
        {tabs && <div className="mt-4 md:mt-5">{tabs}</div>}
      </div>
    </header>
  );
}

export function PageFrame({
  title,
  description,
  actions,
  eyebrow,
  breadcrumb,
  tabs,
  aside,
  bleed = false,
  width = "form",
  children,
}: Omit<FrameHeaderProps, "width"> & {
  width?: PageWidth;
  /**
   * A column beside the body, under the header — Settings' own list of tabs.
   * The aside draws itself for both widths (a column, or a strip on top).
   */
  aside?: React.ReactNode;
  /**
   * For screens that manage their own height — the DAG canvas, the log
   * viewer, the file explorer. Gives them a flex column to fill instead of a
   * scroll container to overflow. `width` does not apply: a bleeding screen
   * owns its own layout all the way to the edges.
   */
  bleed?: boolean;
  children: React.ReactNode;
}) {
  const header = (
    <PageFrameHeader
      title={title}
      description={description}
      actions={actions}
      eyebrow={eyebrow}
      breadcrumb={breadcrumb}
      tabs={tabs}
      bleed={bleed}
      width={aside ? "full" : width}
    />
  );

  if (bleed) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {/* An open canvas is the page: its own floating bar takes the header's
            place (D204, `canvas-mode`). Only a bleeding page holds a canvas. */}
        <CanvasModeHide>{header}</CanvasModeHide>
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{children}</div>
      </div>
    );
  }

  // The header scrolls with the page (Prototype 4). Held still above a scroller
  // with no rule under it, scrolled content was cut off at an edge nobody could
  // see, under the description (Naveen, M1a review). A bleeding page keeps its
  // header still, and its rule shows where the body begins.
  return (
    <div className="h-full min-h-0 overflow-y-auto">
      {header}
      <div className={cn("w-full pb-12", GUTTER, tabs ? "pt-6" : "pt-6 md:pt-8", aside && "lg:flex lg:items-start lg:gap-10")}>
        {aside && <div className="mb-6 lg:sticky lg:top-6 lg:mb-0 lg:shrink-0">{aside}</div>}
        <div className={cn("min-w-0 space-y-10", aside && "lg:flex-1", WIDTHS[width])}>{children}</div>
      </div>
    </div>
  );
}
