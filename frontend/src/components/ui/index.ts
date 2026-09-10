/**
 * The design system.
 *
 * One barrel so every screen imports from `@/components/ui` regardless of how
 * the files are arranged underneath — the split from one `ui.tsx` into several
 * modules, and later the split of `primitives.tsx` into the seven files below,
 * both happened without a call site changing.
 *
 * The dividing line: the presentational files are markup and Tailwind with no
 * interaction model worth outsourcing, and carry no `"use client"` so they
 * render on the server when a server component asks. Everything under the
 * second group wraps a Radix primitive, because focus traps, portalling,
 * type-ahead, `aria-activedescendant` and scroll locking are the parts that are
 * quietly hard to write and very obvious when they are wrong.
 */

export {
  Button,
  IconButton,
  LinkButton,
  buttonStyles,
  type ButtonSize,
  type ButtonVariant,
} from "./button";
export { Badge, Tag, type BadgeTone } from "./badge";
export { BrandLoader, Mark, Spinner, Wordmark } from "./brand";
export { Card, CardBody, CardFooter } from "./card";
export { EmptyState, ErrorNote, SuccessNote } from "./feedback";
export { controlStyles, type ControlSize } from "./control-styles";
export { Field, FieldRow, Input, SearchInput, Textarea } from "./input";
export { TextLink, type TextLinkVariant } from "./link";
export { Markdown } from "./markdown";
export {
  CellStack,
  TBody,
  TD,
  TH,
  THead,
  TR,
  Table,
  TableActions,
  type ColumnWidth,
  type Density,
} from "./table";
export {
  Heading,
  Label,
  Mono,
  Muted,
  PageHeader,
  Stat,
  Text,
  type Tone,
} from "./text";
export { Screen, type ScreenWidth } from "./screen";

export { Checkbox, type CheckedState } from "./checkbox";
export { Section } from "./collapsible";
export { DataTable, type Column } from "./data-table";
export { ConfirmDialog, Modal } from "./dialog";
export { Menu, MenuItem, MenuLabel, MenuLink, MenuSegment, MenuSeparator, RowMenu } from "./menu";
export { Select, type SelectOption } from "./select";
export { Combobox, type ComboboxOption } from "./combobox";
export { StatusBadge, normaliseStatus, type RunStatus } from "./status";
export { Toast, useToast, type ToastTone } from "./toast";
export { Hint, TooltipProvider } from "./tooltip";
export { usePersisted } from "./use-persisted";
