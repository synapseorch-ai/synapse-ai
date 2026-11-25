/**
 * A custom tool's "Name agents use", made from its Name as the person types it (F1739).
 *
 * The editor made it from the Name only while it was empty, so the first letter made it and nothing after: typing
 * "Send email" saved a tool called `s`. It is made again at each keystroke while the tool is new and the name is
 * empty or still the one made from the Name before, and left alone once the person has typed their own. A saved
 * tool's name is never changed by its Name: agents and workflows call the tool by it.
 */

/** "Process Orders" → "process_orders": lower case, each run of other characters one underscore, none at the ends. */
export function slugName(text: string): string {
    return text.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

/** The name agents use once the Name reads `nextGeneral`, from the draft as it was before that keystroke. */
export function derivedName(draft: { name?: string | null; generalName?: string | null },
                            nextGeneral: string, isNew: boolean): string {
    const current = draft.name ?? '';
    if (!isNew) return current;
    return current === '' || current === slugName(draft.generalName ?? '') ? slugName(nextGeneral) : current;
}
