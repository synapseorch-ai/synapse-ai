/*
 * OSS's answers to the "?" panel's questions (`components/help/host.tsx`).
 *
 * A path's topic is its nav entry's id, so a screen added to `lib/nav.ts`
 * without a topic shows the fallback — which ui-smoke's `help_pages` walk fails
 * by name. Cloud keeps its own copy of this file and its own topics; the panel
 * itself is shared.
 */
import { DEFAULT_HELP_HOST, type HelpHost } from '@/components/help';
import { HELP_FALLBACK, HELP_TOPICS } from '@/lib/help';
import { navEntryFor } from '@/lib/nav';

export const ossHelpHost: HelpHost = {
    ...DEFAULT_HELP_HOST,
    topics: HELP_TOPICS,
    topicFor: (pathname) => {
        const id = navEntryFor(pathname)?.id;
        return id && HELP_TOPICS[id] ? id : null;
    },
    fallback: HELP_FALLBACK,
};
