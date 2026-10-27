/*
 * The first things to ask Synapse AI, written for the person (ONB3, D326, F1059; Naveen 2026-10-27: "they're not
 * selecting or understanding the templates", so we write the prompt for them from what they picked).
 *
 * One table, reviewed copy, never model output. Code picks the best one or two for a person (`pick.ts`); a pick
 * lands in a new Synapse AI chat, unsent. Every row is buildable with what any workspace has: the built-in tools
 * (web search, read and scrape a page, screenshots, the Vault, PDFs and spreadsheets) unless `needs` names an app,
 * and then the card says so and Synapse AI asks to connect it after the person's yes. Never a scraping or search
 * vendor: the built-ins do that job (Naveen's built-in-first rule; pinned by both products' tests).
 *
 * Synced into cloud with the rest of `components/starters`. Cloud's tests check every key against its onboarding
 * vocabulary (`api/control/onboarding.py`).
 */

/** Who a prompt is written for: cloud's roles and professions, and OSS Home's "I work in", meet here (`persona.ts`). */
export type Persona =
    | 'founder' | 'operations' | 'marketer' | 'sales' | 'engineer' | 'designer' | 'product' | 'analyst'
    | 'content_creator' | 'editor' | 'anyone';

export type Starter = {
    id: string;
    /** The words put in the composer. `{industry}` is filled from `INDUSTRY_WORDS`; a row with it is skipped without one. */
    text: string;
    /** A few words for the row's title. */
    title: string;
    personas: Persona[];
    /** Onboarding's goal keys (cloud's `CAPABILITIES`). */
    goals: string[];
    /** Onboarding's tool keys an app the prompt needs connected (`TOOLS` in onboarding.py); empty = built-ins only. */
    needs: string[];
};

/** An industry key as it reads inside a sentence ("25 software companies", "the healthcare market"). */
export const INDUSTRY_WORDS: Record<string, string> = {
    software: 'software',
    finance: 'finance',
    ecommerce: 'e-commerce',
    agency: 'agency',
    consulting: 'consulting',
    healthcare: 'healthcare',
    education: 'education',
    media: 'media',
    manufacturing: 'manufacturing',
    logistics: 'logistics',
};

/** How a needed app is named on its row. */
export const NEED_LABELS: Record<string, string> = {
    github: 'GitHub',
    google: 'Gmail',
};

export const STARTERS: Starter[] = [
    {
        id: 'leads', title: 'A lead list in the Vault', personas: ['marketer', 'sales'], goals: ['marketing', 'sales'], needs: [],
        text: 'Make a workflow that searches the web for 25 {industry} companies that fit the customer I describe, with website, size and a one-line reason for each, and saves the list as a spreadsheet in the Vault.',
    },
    {
        id: 'competitors', title: 'A weekly competitor digest', personas: ['marketer', 'founder'], goals: ['marketing', 'research'], needs: [],
        text: 'Make a workflow that runs every Monday: it reads the websites and blogs of the competitors I list and saves a short digest of what changed in the Vault: new pages, pricing, launches.',
    },
    {
        id: 'repurpose', title: 'One post, many posts', personas: ['marketer', 'content_creator'], goals: ['writing', 'marketing'], needs: [],
        text: 'Make a workflow that turns a blog post link I give it into five LinkedIn posts and three short posts for X, and saves them in the Vault for me to review.',
    },
    {
        id: 'audit', title: 'A design audit of a page', personas: ['designer'], goals: ['design'], needs: [],
        text: 'Make a workflow that does a design audit of a web page I give it: screenshots at desktop and phone width, then the top 10 issues in layout, contrast and copy, each with a fix.',
    },
    {
        id: 'site', title: 'A one-page website', personas: ['designer', 'founder'], goals: ['design'], needs: [],
        text: 'Make a workflow that designs a one-page website for a product I describe: it writes the copy and the HTML and saves them in the Vault so I can open it.',
    },
    {
        id: 'prs', title: 'Triage pull requests', personas: ['engineer'], goals: ['development'], needs: ['github'],
        text: 'Make a workflow that connects to GitHub and triages our open pull requests each morning: it labels each by size and risk and lists the ones waiting longest for review.',
    },
    {
        id: 'apiclient', title: 'A client for an API', personas: ['engineer'], goals: ['development', 'custom_api'], needs: [],
        text: 'Make a workflow that reads an API docs page I give it and writes a small client for that API with an example call, saved in the Vault.',
    },
    {
        id: 'callprep', title: 'A brief before each call', personas: ['sales'], goals: ['sales'], needs: [],
        text: 'Make a workflow that researches a company I name before a sales call: what they do, recent news, likely needs and three questions to ask, as a one-page brief.',
    },
    {
        id: 'digest', title: 'A Friday market brief', personas: ['founder'], goals: ['research', 'reporting'], needs: [],
        text: 'Make a workflow that runs every Friday and writes me a one-page brief on the {industry} market: news about our market, competitors and customers, with links.',
    },
    {
        id: 'faq', title: 'An FAQ from our website', personas: ['founder', 'product'], goals: ['support', 'chat'], needs: [],
        text: 'Make a workflow that reads the website I give it and writes a help-centre FAQ of 15 questions and answers from it, saved in the Vault.',
    },
    {
        id: 'invoices', title: 'Invoices into a spreadsheet', personas: ['operations'], goals: ['documents', 'finance'], needs: [],
        text: 'Make a workflow that, when I run it, reads the invoice and receipt PDFs in a Vault folder and adds the vendor, date, amount and due date of each to a spreadsheet there.',
    },
    {
        id: 'inbox', title: 'Sort the morning inbox', personas: ['operations', 'founder'], goals: ['operations'], needs: ['google'],
        text: 'Make a workflow that connects to Gmail and each morning sorts new email into needs reply, FYI and can wait, with a one-line summary of each.',
    },
    {
        id: 'reviews', title: 'Themes in customer reviews', personas: ['product'], goals: ['research'], needs: [],
        text: 'Make a workflow that reads the reviews on a page I give it, groups the feedback into themes and ranks the five most-asked-for changes.',
    },
    {
        id: 'spec', title: 'Notes into a spec', personas: ['product'], goals: ['documents', 'writing'], needs: [],
        text: 'Make a workflow that turns rough feature notes I give it into a one-page spec: the problem, who it’s for, scope and open questions, saved in the Vault.',
    },
    {
        id: 'sheet', title: 'A report from a spreadsheet', personas: ['analyst'], goals: ['data', 'reporting', 'finance'], needs: [],
        text: 'Make a workflow that, when I run it, summarises the newest spreadsheet in the Vault: the key numbers, trends and anything odd, as a short report with a table.',
    },
    {
        id: 'prices', title: 'Track prices weekly', personas: ['analyst'], goals: ['data', 'ecommerce'], needs: [],
        text: 'Make a workflow that runs every week, checks the prices on the product pages I list and keeps a table in the Vault of how they change.',
    },
    {
        id: 'edit', title: 'Edit a draft', personas: ['editor'], goals: ['writing'], needs: [],
        text: 'Make a workflow that edits a draft I give it for clarity and tone, lists each change with its reason, and saves the clean copy in the Vault.',
    },
    {
        id: 'trends', title: 'Ten post ideas a week', personas: ['content_creator'], goals: ['writing', 'marketing'], needs: [],
        text: 'Make a workflow that runs every week, finds 10 topics people in {industry} are talking about and drafts a short post idea for each.',
    },
    {
        id: 'storecheck', title: 'Check our product pages', personas: [], goals: ['ecommerce'], needs: [],
        text: 'Make a workflow that runs every week, checks the product pages I list for broken links, missing prices and missing images, and saves the list in the Vault.',
    },
    {
        id: 'brief', title: 'A researched brief', personas: ['anyone'], goals: ['research'], needs: [],
        text: 'Make a workflow that researches a topic I give it across the web and writes a two-page brief with sources.',
    },
    {
        id: 'summarise', title: 'Summarise a page', personas: ['anyone'], goals: [], needs: [],
        text: 'Make a workflow that summarises any web page I give it into five bullet points and saves them in the Vault.',
    },
    {
        id: 'agent', title: 'An assistant for our website', personas: ['anyone'], goals: ['saas_agents', 'chat', 'support'], needs: [],
        text: 'Make me an assistant that answers questions about the website I give it, using its pages as what it knows.',
    },
];
