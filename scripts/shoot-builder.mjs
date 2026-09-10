#!/usr/bin/env node
/**
 * Screenshot the AI builder's own orchestration for the "See how I'm built" modal.
 *
 * The modal's argument is that the builder is an ordinary orchestration you
 * could have drawn yourself. A picture of it on the real canvas is the whole
 * argument; a hand-drawn diagram would be a claim about the product rather
 * than a look at it.
 *
 * Re-run this whenever `backend/core/native_builder/orchestration.json`
 * changes, or the picture starts describing a builder that no longer exists.
 *
 *   node scripts/shoot-builder.mjs [--url http://localhost:3000]
 *
 * It needs a running instance, because it drives the real canvas. It works on
 * a scratch one — point the backend at a throwaway store first:
 *
 *   SYNAPSE_DB_URL="sqlite+aiosqlite:///tmp/shoot.db" <start the backend>
 *
 * Never run it against a store you care about: it duplicates the builder into
 * the workspace to get an editable copy on the canvas, and deletes that copy
 * afterwards.
 */
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';

const OUT_DIR = path.join(process.cwd(), 'frontend', 'public', 'builder');
const WIDTH = 1600;
const HEIGHT = 900;

function arg(name, fallback) {
    const i = process.argv.indexOf(`--${name}`);
    return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const BASE = arg('url', 'http://localhost:3000').replace(/\/$/, '');

async function loadPlaywright() {
    try {
        return await import('playwright');
    } catch {
        console.error(
            'playwright is not installed.\n' +
            '  npm i -D playwright && npx playwright install chromium\n' +
            'Or set PLAYWRIGHT_CHROMIUM to an existing browser executable.',
        );
        process.exit(1);
    }
}

async function main() {
    const { chromium } = await loadPlaywright();
    await mkdir(OUT_DIR, { recursive: true });

    // A copy of the builder, because the original is hidden from every list —
    // which is the behaviour we want, and the reason the canvas cannot open it.
    const created = await fetch(`${BASE}/api/builder/duplicate`, { method: 'POST' });
    if (!created.ok) {
        console.error(`Could not duplicate the builder (${created.status}). Is the backend running?`);
        process.exit(1);
    }
    const { orchestration_id: orchestrationId } = await created.json();
    console.log(`copied the builder → ${orchestrationId}`);

    const executablePath = process.env.PLAYWRIGHT_CHROMIUM || undefined;
    const browser = await chromium.launch({ executablePath });
    try {
        for (const theme of ['dark', 'light']) {
            const context = await browser.newContext({
                viewport: { width: WIDTH, height: HEIGHT },
                deviceScaleFactor: 2,
            });
            // The app keys light mode off `synapseTheme` in localStorage. The
            // step palette is collapsed the same way: expanded it covers the
            // left third of the canvas, which is where the entry router sits.
            await context.addInitScript(
                (value) => {
                    window.localStorage.setItem('synapseTheme', value);
                    window.localStorage.setItem('orch-palette-collapsed', '1');
                },
                theme,
            );
            const page = await context.newPage();
            // Not `networkidle`: the app holds a notifications stream open on
            // every screen, so the network is never idle and this waited out
            // its whole timeout. The two waits below are the real signal
            // anyway — they wait for the canvas rather than for the socket.
            await page.goto(`${BASE}/orchestrations?id=${orchestrationId}`, { waitUntil: 'domcontentloaded' });

            // The canvas lays out asynchronously; wait for the nodes, then for
            // the layout to settle rather than for a fixed number of seconds.
            await page.waitForSelector('.react-flow__node', { timeout: 30_000 });
            await page.waitForFunction(
                (expected) => document.querySelectorAll('.react-flow__node').length >= expected,
                10,
                { timeout: 30_000 },
            );
            await page.keyboard.press('Shift+1').catch(() => {});  // fit view, if bound
            await page.waitForTimeout(1200);

            const canvas = await page.$('.react-flow');
            const file = path.join(OUT_DIR, `how-im-built-${theme}.png`);
            await (canvas ?? page).screenshot({ path: file });
            console.log(`wrote ${path.relative(process.cwd(), file)}`);
            await context.close();
        }
    } finally {
        await browser.close();
        // Leave the workspace as we found it.
        await fetch(`${BASE}/api/orchestrations/${orchestrationId}`, { method: 'DELETE' }).catch(() => {});
        console.log('removed the copy');
    }
}

main().catch((error) => {
    console.error(error);
    process.exit(1);
});
