/**
 * Whether an agent step narrows its agent's tools (F791).
 *
 * `allowed_tools` unset is the agent's own set; a list is the narrowing, an empty one included (the editor's "until
 * you pick at least one tool"). A step saved through the API or the AI builder stores the field as `null`, which is
 * the same as unset: the engine reads it so (`narrow_allowed_tools`), and the editor must too. It read `!== undefined`,
 * so such a step showed "Restrict tools for this step" ticked, with "Nothing selected yet", for a step that restricts
 * nothing.
 */
import type { StepConfig } from '@/types/orchestration';

export function toolsRestricted(step: Pick<StepConfig, 'allowed_tools'>): boolean {
    return step.allowed_tools != null;
}
