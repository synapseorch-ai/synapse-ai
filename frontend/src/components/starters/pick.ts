/*
 * Which starter prompts a person sees (ONB3, D326, F1059): the best one or two rows of `STARTERS` for their answers,
 * chosen in code, the same way every time. Naveen (2026-10-27): "just show one or two … but we'll have many for
 * other roles and things they select".
 */
import { INDUSTRY_WORDS, STARTERS, type Persona, type Starter } from './table';

/** Cloud's onboarding roles (business) and professions (individual) as the table's personas. */
const PERSONA_OF: Record<string, Persona> = {
    // roles
    founder: 'founder',
    operations: 'operations',
    marketing: 'marketer',
    sales: 'sales',
    engineering: 'engineer',
    design: 'designer',
    product: 'product',
    analyst: 'analyst',
    // professions
    developer: 'engineer',
    designer: 'designer',
    content_creator: 'content_creator',
    editor: 'editor',
    marketer: 'marketer',
};

/** A role or profession key (or OSS Home's "I work in" choice) as a persona; anything else reads as anyone. */
export function personaOf(key: string | null | undefined): Persona {
    return PERSONA_OF[key ?? ''] ?? 'anyone';
}

/** OSS Home's "I work in" choices, in the order shown. */
export const WORK_IN: { key: Persona; label: string }[] = [
    { key: 'marketer', label: 'Marketing' },
    { key: 'sales', label: 'Sales' },
    { key: 'engineer', label: 'Engineering' },
    { key: 'designer', label: 'Design' },
    { key: 'operations', label: 'Operations' },
    { key: 'founder', label: 'Founder' },
    { key: 'anyone', label: 'Something else' },
];

export type StarterAnswers = {
    persona: Persona;
    goals?: readonly string[];
    industry?: string | null;
    /** Onboarding's tool keys the person said they use. */
    tools?: readonly string[];
};

/** A row ready to show: its words with the industry filled in, and the apps it needs. */
export type StarterPick = { id: string; title: string; text: string; needs: string[] };

/** `text` with `{industry}` filled, or null when the row needs an industry the answers do not give. */
export function fillStarter(text: string, industry?: string | null): string | null {
    if (!text.includes('{industry}')) return text;
    const words = INDUSTRY_WORDS[industry ?? ''];
    return words ? text.replaceAll('{industry}', words) : null;
}

function score(row: Starter, a: StarterAnswers): number {
    let points = 0;
    if (row.personas.includes(a.persona)) points += 3;
    for (const goal of a.goals ?? []) if (row.goals.includes(goal)) points += 2;
    if (row.text.includes('{industry}')) points += 1;
    for (const need of row.needs) points += a.tools?.includes(need) ? 1 : -2;
    return points;
}

/**
 * The best `n` rows for `answers`, best first; ties keep the table's order. At least one shown row needs no app
 * (built-ins only), so a person can always start without connecting anything.
 */
export function pickStarters(answers: StarterAnswers, n = 2, table: readonly Starter[] = STARTERS): StarterPick[] {
    const ranked = table
        .map((row, i) => ({ row, i, text: fillStarter(row.text, answers.industry), points: score(row, answers) }))
        .filter((r): r is typeof r & { text: string } => r.text !== null)
        .sort((x, y) => y.points - x.points || x.i - y.i);
    const picked: typeof ranked = [];
    for (const r of ranked) {
        if (picked.length >= n) break;
        const last = picked.length === n - 1;
        // The last place goes to a built-in-only row when none is picked yet.
        if (last && r.row.needs.length && picked.every(p => p.row.needs.length)) continue;
        picked.push(r);
    }
    return picked.map(({ row, text }) => ({ id: row.id, title: row.title, text, needs: [...row.needs] }));
}
