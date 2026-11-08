import { Markdown } from '@/components/ui/markdown';

import { cn } from './cn';

// One implementation, in lib/cn.ts, alongside the focus-ring helpers the
// design system uses. Re-exported so the existing `@/lib/utils` call sites keep
// working.
export { cn };

/**
 * Render assistant text as prose.
 *
 * The renderer itself is `components/ui/markdown` now — it was inlined here,
 * and a second copy lived in the orchestration tab. This stays as the name nine
 * call sites already import; it is not worth touching those to spell the same
 * thing differently.
 *
 * The wrapping `<div className="markdown-content">` that used to be here is
 * gone. Nothing in the repo styled that class — not `globals.css`, not a
 * module — so it was a hook with nothing on the other end of it.
 */
export const renderTextContent = (content: string) => <Markdown>{content}</Markdown>;
