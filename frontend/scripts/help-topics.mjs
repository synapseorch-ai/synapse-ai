// The product's own help, for the AI builder's `search_help` (BWA1, D274).
//
//   node --experimental-strip-types frontend/scripts/help-topics.mjs > backend/core/native_builder/help_topics.json
//
// Reads `src/lib/help.ts` (the "?" panel's words, H1) and the path of each topic's screen from `src/lib/nav.ts`, and
// writes them as JSON the backend can read: a how-to question is answered from the product's own words, not a model's
// memory of a product (BWA1's baseline: "Settings → Integrations", a screen that does not exist). Re-run after any
// change to help.ts; `backend/tests/unit/test_the_builder_answers_how_to_from_the_help.py` fails until you do.
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const { HELP_TOPICS } = await import(join(here, '..', 'src', 'lib', 'help.ts'));

// `nav.ts` imports icons at runtime, so its entries are read as text: `id: 'x', label: 'Y', href: '/z'`.
const nav = readFileSync(join(here, '..', 'src', 'lib', 'nav.ts'), 'utf8');
const where = {};
for (const m of nav.matchAll(/id:\s*'([^']+)',\s*label:\s*'([^']+)',\s*href:\s*'([^']+)'/g)) {
    where[m[1]] = { label: m[2], href: m[3] };
}

const out = {};
for (const [key, topic] of Object.entries(HELP_TOPICS).sort(([a], [b]) => a.localeCompare(b))) {
    out[key] = {
        title: topic.title,
        summary: topic.summary,
        steps: topic.steps ?? [],
        ...(topic.guide ? { guide: topic.guide } : {}),
        ...(where[key] ? { where: where[key] } : {}),
    };
}
process.stdout.write(JSON.stringify(out, null, 1) + '\n');
