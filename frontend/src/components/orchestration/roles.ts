/**
 * The canvas's colours, by who does the work (D211, M3a).
 *
 * Cyan is the product acting (a step that calls a model), amber is a person,
 * grey is logic. It replaces the 16 hues in `STEP_TYPE_META`, which stay
 * exported for anything that still reads them but are no longer drawn. Which
 * role a type has is `roleOf` in the run screens' model, so the canvas and the
 * run page (its path glyph, its waterfall) can never disagree. A step's icon
 * tile is the run screens' `StepIcon`, for the same reason.
 */
import { roleOf, type StepRole } from '../run/model';

export { roleOf, type StepRole };

/**
 * The minimap's fill. xyflow sets it as `style.fill`, so a token expression
 * follows the theme where a hex would not.
 */
export const ROLE_FILL: Record<StepRole, string> = {
    model: 'color-mix(in oklab, var(--machine) 42%, var(--surface-2))',
    logic: 'var(--border-strong)',
    person: 'color-mix(in oklab, var(--person) 52%, var(--surface-2))',
};

/** The words a screen reader and the card's chip use for a role. */
export const ROLE_NAME: Record<StepRole, string> = {
    model: 'Calls a model',
    logic: 'Logic',
    person: 'A person',
};
