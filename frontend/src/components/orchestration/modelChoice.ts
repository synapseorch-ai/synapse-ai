/**
 * How to choose a model for an agent or a step (F1598). Their pickers keep every model and show each one's context,
 * cost tier and price; this says what to weigh. It lives beside the step editor, which cloud takes as it is, so both
 * products say the same.
 */
export function chooseForTheJob(what: 'agent' | 'step'): string {
    return `Choose for the job this ${what} does: a larger context for long documents or many pages, `
        + 'a stronger model for careful judgement or writing, a smaller, cheaper one for short, simple steps.';
}
