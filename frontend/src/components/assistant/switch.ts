/*
 * Build with AI's way in for someone leaving OpenAI Agent Builder (SW1, D253).
 *
 * Agent Builder shuts down on 2026-11-30, and its only way out is its Code
 * button: Agents SDK code, Python or TypeScript. The person pastes that code
 * under the lead line and the builder reads it — the server recognises the
 * code however it arrives, so this is a door, not a mode.
 *
 * Both products' panels offer it as a suggestion under the empty composer, and
 * both open with it from `?build=agent-builder`, the link the site's /switch
 * page and sign-up send people to.
 */
export const AGENT_BUILDER_SWITCH = {
    /** The `build` query value that opens the builder ready for the paste. */
    param: 'agent-builder',
    /** The suggestion under the empty composer. */
    suggestion: 'Move a workflow from OpenAI Agent Builder',
    /** What the composer holds: the paste goes underneath. */
    lead: 'Rebuild this OpenAI Agent Builder workflow here. Its export (Code → Agents SDK) is below:\n\n',
} as const;

/**
 * Whether `text` is the import starter's line and nothing else (F1607): the panel holds it back and says where the export
 * comes from. Anything under the line, code or not, goes to the server, which decides.
 */
export function isBareImportLead(text: string): boolean {
    return text.trim() === AGENT_BUILDER_SWITCH.lead.trim();
}

/**
 * What the panel says when it holds the bare line back: true before and after Agent Builder closes (F1636), since a
 * saved export is still one and the person's own words always do.
 */
export const IMPORT_LEAD_NOTE = 'Paste the workflow\'s Agents SDK code under the line first (the Code → Agents SDK export from Agent Builder, or a copy you saved), attach a screenshot of it, or describe the workflow in your own words.';

/** Whether `search` (a `location.search`) asks for the switch. */
export function asksForAgentBuilderSwitch(search: string): boolean {
    return new URLSearchParams(search).get('build') === AGENT_BUILDER_SWITCH.param;
}

/** `href` without its `build` parameter, so a reload does not open it again. */
export function withoutBuildParam(href: string): string {
    const url = new URL(href);
    url.searchParams.delete('build');
    return `${url.pathname}${url.search}${url.hash}`;
}
