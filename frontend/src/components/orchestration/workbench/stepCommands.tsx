'use client';
/**
 * The step list, as ⌘K items (D211: steps are added from ⌘K / Add step, and a
 * type can still be dragged onto an exact spot, as from the old palette).
 *
 * The dock's Add step shows it grouped as the palette was (Core, Logic, Data,
 * I/O); the app's ⌘K shows it as one "Add a step" group while a canvas is open.
 * Every item can be picked (the step goes after the selected one, where the
 * palette's click put it) or dragged (it lands under the cursor).
 */
import type { CommandGroup, CommandItem } from '@/components/ui';
import { STEP_TYPE_META, type StepCategory, type StepType } from '@/types/orchestration';
import { StepIcon } from '../../run/StepIcon';
import { STEP_DRAG_TYPE } from '../StepPalette';

const CATEGORY_ORDER: StepCategory[] = ['Core', 'Logic', 'Data', 'I/O'];

/** Why a Transform step cannot be added where code does not run: the step card's words and the list's (F882). */
export const TRANSFORM_NEEDS_CODE = 'Runs Python — available on Team, Growth and Business.';

function item(
    type: StepType, idPrefix: string, onAdd: (type: StepType) => void, disabledBecause?: string,
): CommandItem {
    const meta = STEP_TYPE_META[type];
    return {
        id: `${idPrefix}${type}`,
        label: meta.label,
        hint: disabledBecause ?? meta.blurb,
        disabled: Boolean(disabledBecause),
        icon: <StepIcon type={type} className="size-7 [&_svg]:size-4" />,
        keywords: [type.replace('_', ' '), meta.category],
        onSelect: () => onAdd(type),
        drag: { type: STEP_DRAG_TYPE, value: type },
    };
}

export function stepCommandGroups({ onAdd, hiddenTypes, disabledTypes, grouped }: {
    onAdd: (type: StepType) => void;
    hiddenTypes?: readonly StepType[];
    /**
     * Types shown but not offered, each with why (F882): on a plan without code, Transform — offered, it was
     * added and the whole workflow's Save then refused with a 402, though the step card's own picker knew.
     */
    disabledTypes?: Partial<Record<StepType, string>>;
    /** By category for the dock's list; one "Add a step" group for the app's ⌘K. */
    grouped: boolean;
}): CommandGroup[] {
    const hidden = new Set(hiddenTypes ?? []);
    const types = (Object.keys(STEP_TYPE_META) as StepType[]).filter((t) => !hidden.has(t));
    const make = (t: StepType, prefix: string) => item(t, prefix, onAdd, disabledTypes?.[t]);
    if (!grouped) {
        return [{ heading: 'Add a step', items: types.map((t) => make(t, 'add-step:')) }];
    }
    return CATEGORY_ORDER.map((category) => ({
        heading: category,
        items: types.filter((t) => STEP_TYPE_META[t].category === category).map((t) => make(t, 'step:')),
    }));
}
