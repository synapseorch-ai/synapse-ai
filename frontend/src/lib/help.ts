/*
 * What the "?" says on each OSS page (H1, D192). Cloud keeps its own topics.
 *
 * Keyed by the page's nav id (`lib/nav.ts`), plus the states a path does not
 * name: a workflow open in the editor, a knowledge base open. A topic's
 * title is the page's own heading, which ui-smoke's `help_pages` walk checks.
 *
 * Written from the screens, not from memory: labels are quoted as the code
 * spells them (the stylesheet may draw them in capitals), and a step promises
 * nothing the screen does not do — OSS reads no scanned pages, for one.
 *
 * `media` ids are HC0's shots on the docs site (pro `scripts/docs-media/
 * shots.yaml`, `used_by`); `oss-` marks a screen OSS draws differently from
 * cloud. `guide` paths are G1's guides (pro `MEMORY/drafts/h1/guides.md`).
 */
import type { HelpTopic } from '@/components/help';

export const HELP_FALLBACK = 'app';

export const HELP_TOPICS: Record<string, HelpTopic> = {
    app: {
        title: 'Getting around',
        summary: 'The menu on the left has nine items. Pages that belong together are tabs under one of them: Runs holds Logs, Evals and Usage, for one.',
        steps: [
            'Use Home and Runs to see what is running, what ran, what it said and what it cost.',
            'Under Build, make Workflows (with Schedules and Templates) and Agents (with Skills), keep files in the Vault, search them with Knowledge when it is on, and Chat.',
            'Under Connect, give agents Apps and Tools to call, and Channels and Widgets to meet people on.',
            'Press Ctrl+K (⌘K on a Mac), or click “Jump to…”, to go to any page or tab by name.',
            'Click “Synapse AI” in the menu to ask about your workspace or build with AI from any page; on a phone, the mark in the top bar. Its clock button lists your Synapse AI chats to pick one up again, and the bin starts a new one. Settings sits at the foot.',
        ],
        media: { id: 'start/menu-and-jump', alt: 'The menu down the left — Home, Runs, then Build and Connect — with Jump to open over the page, listing every page and tab by name.' },
        guide: '/guides/start/how-it-fits',
    },

    // ── Overview ────────────────────────────────────────────────────────────
    dashboard: {
        title: 'Home',
        summary: 'What needs you first, then how the week went: runs waiting on an answer, the week’s figures and runs, what is coming up, and what your model calls have cost.',
        steps: [
            'Answer what is under “Needs you” right there: approve or decline a run’s question, or a scheduled agent’s tool call. “Open run” shows the whole run.',
            'Read the strip: what is running now, what waits on you, and how many runs failed or completed in the last seven days. Each bar below is one run of the week.',
            'Open a run from “Active now” or “Recent runs”, or click “All runs” for every run; “Coming up” lists the next scheduled runs.',
            'Read “Total spend”, “Requests”, “Tokens” and “Cache saved” over every recorded model call, and “Spend by model” for which models cost most; “Add one” sets a missing price.',
            'Click a row under “Workspace” to go to your workflows, agents, schedules or apps.',
        ],
        media: { id: 'home/oss-dashboard', alt: 'The Home page: nothing needs you, Getting started folded to its next step, the week’s figures, recent runs and what is coming up.' },
        guide: '/guides/start/how-it-fits',
    },
    runs: {
        title: 'Runs',
        summary: 'Every run of a workflow, newest first and grouped by day: what started it, the path of steps it follows, how long it took, what it cost and how it scored.',
        steps: [
            'Pick “Needs you”, “Running”, “Failed” or another status at the top to show only those runs; each counts the latest 100.',
            'Type in “Search runs…” to find a run by its workflow, its run ID or what started it.',
            'Read a row’s path: one bar per step, cyan where it calls a model, amber where a person decides, grey for logic.',
            'Click a run to open its page: the path it took, why it failed, and the question it waits on.',
            'For the full transcript of what each model was sent, open Logs, the tab beside this one.',
        ],
        guide: '/guides/start/runs',
    },
    'runs.detail': {
        title: 'Run details',
        summary: 'One run of a workflow, as the path it took: what it did step by step, how long each step took, what it produced and cost, and why it stopped.',
        steps: [
            'If it is waiting for a person, fill in “Your answer” or the fields shown, then press “Send answer”.',
            'If it failed, read the sentence at the top for why and what to do; “Technical details” holds the step’s own words.',
            'Press “Replay” to resume a failed or cancelled run from where it stopped, or “Cancel” to stop a running one.',
            'Open a step on the path to read its output; “Activity” follows what the run is doing now.',
            'Press “Open in editor” to see the run on its workflow’s canvas; open “Shared state”, “Raw event log” or “Full log” for the rest.',
        ],
        guide: '/guides/start/runs',
    },
    logs: {
        title: 'Logs',
        summary: 'The full log of every agent, workflow and schedule run, grouped by session, so you can see what a run actually did and in what order.',
        steps: [
            'Choose “Agent logs”, “Workflow logs” or “Schedule logs” at the top; the newest log opens beside the list.',
            'Use “Filter” to show one agent, workflow or schedule; it appears once the list holds more than one.',
            'A workflow’s log shows how its run went, how long it took and what it cost; a run another run started sits beneath it.',
            'Use “Transcript sections” to jump to a model call, a step or a tool call, and “Copy” to take the whole log.',
            'Click “Load more” for older logs, or Refresh for new ones. Deleting a log, from its row, is immediate and cannot be undone.',
        ],
        media: { id: 'runs/oss-runs-logs', alt: 'The Logs tab of Runs, showing Workflow logs: the newest run’s log open beside the list, with agent and schedule logs a filter apart.' },
        guide: '/guides/start/runs',
    },
    evals: {
        title: 'Evals',
        summary: 'Evals check that your workflows keep doing a good job. A rubric says what good looks like, and a model scores each run against it and tells you when quality drops. Golden sets replay the same test inputs after a change.',
        steps: [
            'Click “New rubric”, add criteria with “Add criterion”, set “Lowest OK score” and “Allowed drop”, and click “Save rubric”.',
            'In Workflows, open one, open its workflow settings and choose the rubric under “Score runs against”.',
            'Open the rubric and pick the workflow in “Scores for”: the latest and average scores, how many passed, each criterion and the scored runs, where you say whether you agree.',
            'If it says “Nothing scored yet”, click “Score earlier runs” to judge runs from before the rubric.',
            'Click “New set” under Golden sets, add cases and “Save set”; “Run” tells you the cost before it starts.',
        ],
        media: { id: 'evals/rubric', alt: 'The Evals tab of Runs with a rubric open: its latest and average score, how many runs passed, each run as a point, and each criterion’s average.' },
        guide: '/guides/start/evals',
    },

    usage: {
        title: 'Usage',
        summary: 'What your AI models have cost: the total, what reuse saved, cost by model and by session, and the prices the estimates use.',
        steps: [
            'Read “Total cost”, “Model calls”, “Words in” and “Words out”; costs are estimates from the prices below.',
            'Check “Cache analytics” for what reusing earlier work saved, by model and by workflow run.',
            'Under “Session history”, pick a “Filter by” option and click a session for its turn-by-turn cost.',
            'In “Prices used for estimates”, edit a rate, or pick “Custom model ID…” to add one, then “Save pricing” to match what you pay.',
            '“Clear logs” deletes every usage record once you confirm; it cannot be undone.',
        ],
        media: { id: 'usage/overview', alt: 'The Usage tab of Runs: model spend, calls and tokens, and the cost by model.' },
        guide: '/guides/start/runs',
    },

    // ── Build ───────────────────────────────────────────────────────────────
    orchestrations: {
        title: 'Workflows',
        summary: 'Your workflows, each a row: what starts it, how its last run went, and its steps by who does the work — a step that asks a person is the amber one. Open one to edit it on the canvas.',
        steps: [
            'Press “New workflow” for a blank canvas, “Build with AI” to describe one to Synapse AI, or “Templates” to start from a ready-made one.',
            'Click a workflow’s row to open it on the canvas; its ⋯ menu duplicates it, copies its ID or deletes it.',
            'The chip says what starts it — a schedule’s time on its own clock, or Manual — and the row shows its last run’s status, time, duration and cost.',
            'Search by name or description, or sort by name, steps, last run, trigger or when it was updated.',
            'Switch to “Active”, “Recent” or “All runs” for runs; click one to open it on its canvas. “Needs input” means it is waiting for you.',
        ],
        media: { id: 'workflows/list', alt: 'The Workflows page: each workflow with what starts it, how its last run went and its steps by role; Schedules and Templates are tabs beside it.' },
        guide: '/guides/start/workflows',
    },
    'orchestrations.editor': {
        title: 'Editing a workflow',
        summary: 'The workflow fills the canvas and its tools float over it: the bar at the top, the setup card at the right, the dock at the bottom and the run strip at the bottom left.',
        steps: [
            'Press “Add step” in the dock (or ⌘K) and pick a step, or drag one onto the spot you want; drag between handles to connect steps.',
            'Click a step to set it up in the card on the right: its name, its agent or prompt, its output key and the next step. Esc or a click on empty canvas puts the card away.',
            'Open “Workflow settings” for the description, its schedules, the shared state keys, what a run costs and a cap on it, the turn and time limits, the result and the rubric that scores runs.',
            'Click “Save” or press Ctrl+S; anything to fix shows on the step and under “to check” in the bar.',
            'Click “Run now” (its arrow takes an input for the run): it runs the last saved version. The run strip says where it is, and “Run Log” opens what it did.',
        ],
        media: { id: 'workflows/add-step', clip: true, alt: 'Adding an LLM step from Add step; the new step appears on the canvas with its settings open.' },
        guide: '/guides/start/first-workflow',
    },
    agents: {
        title: 'Agents',
        summary: 'The agents that do the work: their instructions, model, tools and skills. An agent can be chatted with, scheduled, or used as a step in a workflow.',
        steps: [
            'Click “New agent” for a new one, or the arrow beside it and “Build Agent with AI…” to describe one and let AI build it. The first agent opens on its own.',
            'On “Setup”, set its name, description and model; “Use default” follows Settings → Models.',
            'Under “Instructions”, describe the agent in “AI Prompt Writer” and click “Generate”, or write the instructions yourself.',
            'Turn on what it may use under “Tools” (and its Tool Policy), then its skills and, when shown, knowledge bases. “Can send helpers” lets it hand a focused task to a short-lived helper with some of its tools; “Helper model” is what they run on, the agent’s own unless you pick another.',
            'Click “Save” in the bar at the foot. Try it in the test chat on the right, which talks to the saved agent; “Runs”, “Metrics” and “Used by” show what it ran, what it cost and where it is used.',
        ],
        media: { id: 'agents/oss-editor', alt: 'An agent open: its name and description, its Setup, Messaging Channels, Runs, Metrics and Used by tabs, its model, and a test chat beside it.' },
        guide: '/guides/start/agents',
    },
    skills: {
        title: 'Skills',
        summary: 'A skill is a named set of instructions. An agent sees each of its skills’ descriptions every turn, and reads the full instructions only when a task calls for them.',
        steps: [
            'Click “New skill”, then give it a lower-case name and a one-line description of when it applies.',
            'Write the instructions in Markdown, check them under “Preview”, and click “Save skill”.',
            'Pick a skill in the list to read it, and click Edit to change it; editing a “Disk” skill makes a “Library” copy that takes its place.',
            'In Agents, click the skill under “Skills” on an agent’s “Setup”, then click “Save”.',
        ],
        media: { id: 'skills/list', alt: 'The Skills tab of Agents with a skill open: its instructions for how agents write payment notes.' },
        guide: '/guides/start/agents',
    },
    browser: {
        title: 'Browser',
        summary: 'Sites your agents stay signed in to, kept as each site’s session and never your password.',
        steps: [
            'Click “Keep me signed in to a site”, name it, and sign in in the browser that opens; only you drive it, and nothing is recorded.',
            'Click “Keep” when you are in. Synapse keeps the site’s session, encrypted: never your password, which agents never see or type.',
            'A session expires in 30 days at most, or sooner when the site ends it; the row says when, and an expired one offers “Sign in again”.',
            'Pick sessions for an agent in its Browser section, one per site. “Remove” takes a session away at once, and says first what uses it.',
        ],
        // The browser's own guide is written with the docs at the announcement (outline in pro drafts/brw2).
        guide: '/guides/start/agents',
    },
    'browser.recordings': {
        title: 'Recordings',
        summary: 'Tasks done once in the browser while Synapse recorded them, for workflows to repeat. Each thing you did is a step with its picture; passwords, card details and codes are never kept, so those steps stay yours to do.',
        steps: [
            'Click “Record a task”, pick the “Sessions it starts with” and where to “Start at”, and click “Open the browser”. Do the task once as you normally would, then “Stop and keep”, or “Discard”.',
            'On its page, “Make them inputs” turns what you typed into values asked for on each run, and “Keep them fixed” types them as recorded; a step’s “Make it an input” or “Keep it fixed” changes one later.',
            'Open a step’s “Details” for what it acts on, “Rename” it, or use its ⋯ menu to “Re-record from here” or “Delete step”.',
            'A Browser agent’s fix, or steps you re-recorded, wait on the recording’s page, and its row in Recordings says “A fix is waiting”: “Approve fix” makes it the next version, “Discard fix” drops it, and “Versions” keeps the ones before.',
            'Use it in a workflow’s Browser step. “Delete recording” deletes it, its steps and their pictures at once, and nothing can repeat it after.',
        ],
        // As `browser`: the full guide comes with the docs pass.
        guide: '/guides/start/agents',
    },
    'orchestrations.browser_step': {
        title: 'A Browser step',
        summary: 'A recording repeated by code in the run’s own browser, with nobody watching. A step that’s yours to do, a send it may not make, or a page that changed pauses the run for you, unless a Browser agent may find the way.',
        steps: [
            'Click “Add step” and pick Browser, then pick a “Recording”, or “Record a new one” to make it here; one made here is this workflow’s.',
            'Under “Inputs”, say what it types for each one: {{ state.key }} from the run, or text.',
            'Under “Sends”, a click or press that sends something waits for you; tick “May send on its own” only for one that may go without you.',
            'Under “If a page changed”, choose “Pause for you” to do that step yourself in the run’s browser, or “Let a Browser agent find the way” and pick the agent; its fix waits on the recording’s page for “Approve fix”.',
            'Pick “Sessions for this step”, one per site: the run’s browser starts signed in with them.',
        ],
        guide: '/guides/start/workflows',
    },
    'agents.editor': {
        title: 'Setting up an agent',
        summary: 'An agent is a model with instructions and tools. Say what it is for, pick what it may use, then try it in the chat beside its setup before you put it to work.',
        steps: [
            'Under “Model”, pick the model it thinks with; a stronger one costs more per answer.',
            'Under “Instructions”, say in plain words what it does, for whom, and what it must never do.',
            'Under “Tools”, tick what it may use: apps, your own tools, the Vault, the web.',
            'Ask it something in the test chat to see how it answers, then save.',
            'Use it in Chat, or as a step in a workflow.',
        ],
        guide: '/guides/start/agents',
    },
    chat: {
        title: 'Chat',
        summary: 'Talk to your agents and watch them work: their thinking, the tools they call and what they return. Each conversation is a tab, so several can be open and working at once, and every one is kept for you to pick up again.',
        steps: [
            'Click + beside the tabs, or press Alt+T, and pick who to talk to: the conversation opens in a tab of its own. Alt+1 to 9 goes to a tab, Alt+W closes the one in front, and a tab can be dragged to another place. A tab keeps its agent: picking another agent at the foot of the message box changes it while the conversation is empty, and opens the agent in a new tab once it has started. Synapse AI too: each new Synapse AI tab is a new chat, and a chat open in the side panel and in a tab is the same chat.',
            'A tab behind keeps working: it shows a spinner while its agent works, an amber dot when it needs you, a tick when your answer has arrived, and a bold title with a dot when a message you didn’t ask for has. Up to 10 tabs stay open; past that the one you used least long ago closes, never one that is working or waiting on you.',
            'Type your message and press Enter; Shift+Enter adds a line, and “Stop” halts the agent. Attach, paste or drop images when the agent’s model can read them; a workflow agent takes them when one of its steps has “Sees attachments” on.',
            'An agent that can send helpers shows each one as a line beside its answer, with its own “Stop” while it works; open the line to see what it did, and “Save as agent” there opens a new agent made from it, saved only when you click “Save”. One sent to work in the background posts its result here when it finishes, marked “From your helper”; helpers that finish close together post one reply between them.',
            'History, Pinned and Search are on the right, Synapse AI’s chats among them: open an earlier conversation in a tab, pin one to find it again, search every question and answer, or delete one. On a phone, the button above the conversation lists your tabs.',
        ],
        media: { id: 'chat/oss-home', alt: 'Chat: a conversation with the Order desk agent, whose name sits at the foot of the message box.' },
        guide: '/guides/start/chat',
    },
    schedules: {
        title: 'Schedules',
        summary: 'Run an agent or a workflow on its own, every so often or at set times, with a standing prompt.',
        steps: [
            'Click “New schedule”, name it, choose “Agent” or “Workflow” under “Run as”, and pick which one.',
            'Write the “Prompt”: the instruction it is given every time the schedule fires.',
            'Choose “Interval” and how often, or “Fixed time (cron)” with a preset and an hour, on your own clock (the hour says its time zone), and the words under the expression say when it runs.',
            'Leave it enabled and click “Create schedule”. Each row shows how often it runs, when it runs next and how its last run went; its switch pauses it, and it can be run now, edited or deleted.',
            'Each run’s log is in Runs › Logs, under “Schedule logs”.',
        ],
        media: { id: 'schedules/list', alt: 'The Schedules tab of Workflows: a schedule that runs a workflow every Monday at 07:00 UTC, with its next and last run.' },
        guide: '/guides/recipes/schedules-and-channels',
    },


    templates: {
        title: 'Templates',
        summary: 'Ready-made packs of agents, workflows and apps. You see everything a pack holds before anything is imported.',
        steps: [
            'Each pack shows its workflows step by step, and says when you approve before one finishes. Click “Preview & Import” to see what it brings.',
            'Untick anything you do not want; what the rest needs is kept.',
            'Click “Import Now”, or “Configure Secrets →” when servers or tools need secret values.',
            'Find what it imported under Workflows, Agents and Apps; “All templates” goes back without importing.',
        ],
        guide: '/guides/start/setup',
    },

    // ── Tools ───────────────────────────────────────────────────────────────
    custom_tools: {
        title: 'Tools',
        summary: 'Tools let agents act in other systems: call an API, run Python, or start an n8n workflow. Each one says in plain words when an agent should use it, and what it needs.',
        steps: [
            'Click a tool in the list to edit it beside the list, or click “New tool”, name it, and fill in “When should an agent use this?”.',
            'For HTTP, choose a method, enter the URL and list its arguments in “What it needs (JSON schema)”.',
            'For n8n, connect it in Settings → Integrations first, then pick the workflow under “n8n Workflow”.',
            'For Python, write code that prints its result on the Python tab and try it with “Run”; it needs Docker.',
            'Click “Save”, then tick the tool under “Tools” on an agent’s “Setup”. “Import from an API description” makes one tool for each action an API lists.',
        ],
        media: { id: 'tools/list', alt: 'The Tools tab of Apps with a Python tool open, which works out when an invoice is due.' },
        guide: '/guides/start/tools',
    },
    mcp_servers: {
        title: 'Apps',
        summary: 'Connect the apps your agents work with, and their actions become tools your agents can use. An app can run on this machine or be reached by its web address.',
        steps: [
            'Google Workspace is connected in Integrations; the card at the top goes there. For another app, choose “On this machine (stdio)” or “By web address (URL)”, and click a preset to fill in the form, or fill it in yourself.',
            'Enter a “Name” and a “Short id (no spaces)”; for an app on this machine, its command, arguments and environment.',
            'For an app by web address, enter the “Server URL”; leave the access token empty to sign in instead.',
            'Click “Connect”, then tick its tools under “Tools” on an agent’s “Setup”.',
            'If other people can reach this install over a network, turn apps on this machine off or limit their commands.',
        ],
        guide: '/guides/start/tools',
    },
    vault: {
        title: 'Vault',
        summary: 'Files your agents can use when a prompt names them with @[path]. Write Markdown, JSON and text files here, in folders.',
        steps: [
            'Use the new folder and new file buttons above the tree; a file is Markdown, JSON or plain text.',
            'Pick a file to edit it, and click “Save” while it shows “Unsaved”.',
            'In an agent’s system prompt or a step’s prompt, type @ and pick a file; it goes in as @[path].',
            'With Knowledge on, a folder that is a knowledge base shows a “Knowledge” badge; make one in Knowledge.',
        ],
        media: { id: 'vault/browse', alt: 'The Vault: the compliance guides folder open in the tree, and a saved web page read beside it.' },
        guide: '/guides/start/tools',
    },
    knowledge: {
        title: 'Knowledge',
        summary: 'Knowledge lets agents answer from your own documents and say where the answer came from. Each knowledge base is one Vault folder.',
        steps: [
            'If it says Knowledge is off, turn on “Knowledge” in Settings → General and click “Save changes”.',
            'In Settings → Models, choose an embedding model for knowledge; without one, search matches words only.',
            'Click “New knowledge base”, name it, and pick a Vault folder or type a new one.',
            'Choose a “Preset” — “Cheap”, “Balanced” or “High recall” — and click “Create”.',
            'Files already in the folder start indexing. Select the base to add more and follow each file.',
        ],
        media: { id: 'knowledge/bases', alt: 'Knowledge: the knowledge bases on the left, and one open on its numbers, with Upload files, Add web pages, Test search and its documents.' },
        guide: '/guides/knowledge/bases',
    },
    'knowledge.base': {
        title: 'Working in a knowledge base',
        summary: 'Add files and web pages to a knowledge base, see how each one was read, and test what your agents will find in it.',
        steps: [
            'Click “Upload files” or drop files on the dashed area. Documents, slides, spreadsheets and code are read; scanned pages are not.',
            'Click “Add web pages”, paste the addresses, and choose pages, sitemaps or whole sites.',
            'Click a file for its report: what happened at each stage, and any warnings. “Retry” reads it again. “Files”, “Web pages” and “Records” show one kind.',
            'Under the base’s numbers, ask in “Test search” what an agent would ask, to see the passages it would get.',
            'Click “Settings” for the preset, search and budget; “Costs” shows this month’s spend, and “Evaluate” how often it finds the answer.',
        ],
        media: { id: 'knowledge/upload', clip: true, alt: 'Uploading a policy document to a knowledge base; it is read and indexed, ready for agents to search.' },
        guide: '/guides/knowledge/sources',
    },
    boards: {
        title: 'Boards',
        summary: 'Cards of shared work that agents read, claim and close. Each workflow and each scheduled agent has its own board, kept across runs; a shared board is used by what its list names.',
        steps: [
            'If it says Boards are off, turn on “Boards” in Settings → General and click “Save changes”.',
            'A workflow’s or a schedule’s board starts at its first card: its agents add cards as they work, or you add one.',
            'Click “New shared board” for work between workflows and schedules, then name who may use it under “Who can use this board”.',
            'An agent’s own board is on unless its Boards section turns it off; its shared boards come from each board’s list, at most 3.',
        ],
        guide: '/boards',
    },
    'boards.board': {
        title: 'Working on a board',
        summary: 'A board’s open cards come first — a workflow’s current run before earlier runs — then what is done. Click a card to read it, change it, close it or take a change back.',
        steps: [
            'Click “Add a card” for a task (work to do) or a note (something to know). Your card is an instruction the next run reads first, marked in amber.',
            '“From content” marks a card written after an agent read outside content; agents that read it are treated the same way.',
            'Open a card for its comments and history; “Undo” takes back any change, an agent’s or yours, for 30 days.',
            'Search finds cards by title, text or key, archived ones too.',
        ],
        guide: '/boards/tasks',
    },
    'orchestrations.board': {
        title: 'A workflow’s board',
        summary: 'What this workflow’s agents leave for each other and for its next run, opened from “Board” in the canvas’s top bar.',
        steps: [
            'This run’s cards come first, then earlier runs’. Only this workflow’s agents see the board.',
            'Click “Add a card” to tell the next run something; it reads your card first.',
            'Every board, shared ones too, is in Knowledge → Boards.',
        ],
        guide: '/boards/agents',
    },

    // ── Settings ────────────────────────────────────────────────────────────
    general: {
        title: 'General',
        summary: 'How the whole workspace behaves: the agent’s name, Knowledge, long outputs and histories, tool policy, database writes, sandboxing and sign-in.',
        steps: [
            'Set the “Global agent name”, and turn on “Knowledge” to make Vault folders searchable.',
            'Use “Large response handling” and “Auto context compaction” to keep long outputs and histories in bounds.',
            'Under “Tool policy”, choose “Allow”, “Ask first” or “Block” for each kind of tool.',
            'Keep “Database write access” off unless agents must write, and list the folders the bash tool may use.',
            'Click “Save changes”. To require a password, turn on “Require login” and click “Save login settings”.',
        ],
        media: { id: 'settings/oss-general', alt: 'Settings → General, with the switch that turns Knowledge on.' },
        guide: '/guides/start/setup',
    },
    models: {
        title: 'Models',
        summary: 'Bring your own keys for the model providers you use, then choose the models the workspace uses. Agents with no model of their own use the default.',
        steps: [
            'Under “Providers”, click “Add key” on one and paste its key; the link inside says where to get one. Local models and CLI models need no key.',
            'A provider with a model count is ready; “No key configured”, “Not running” or “Not installed” says what it needs.',
            'Choose the “Default model”, and a “Builder model” if Synapse AI should use another.',
            'Set “Reasoning effort” and “Max response length” for agents that set neither.',
            'Choose an “Embedding model” and, with Knowledge on, its models; then click “Save changes”.',
        ],
        media: { id: 'settings/oss-models', alt: 'Settings → Models: the default model and a key for each provider.' },
        guide: '/guides/start/setup',
    },
    personal_details: {
        title: 'Personal details',
        summary: 'Your name, contact details and address, kept so an agent can fill in a form or a billing address for you when a task needs it.',
        steps: [
            'Fill in your name, email and phone number, and the address if tasks need one.',
            'Leave a field empty if you would rather agents did not have it, and click “Save changes”.',
            'In Agents, tick “Personal Details” under “Tools” on the “Setup” of each agent that should read them.',
        ],
        media: { id: 'settings/oss-profile', alt: 'Settings → Personal details: who you are, for your agents to use.' },
        guide: '/guides/start/setup',
    },
    variables: {
        title: 'Variables',
        summary: 'Values you set once and use as {{ global.key }} in a tool’s URL, headers or body, an agent’s system prompt, or a step’s prompt.',
        steps: [
            'Click “Add variable”, then enter a name, such as api_base, and its value.',
            'Names use letters, numbers, hyphens and underscores, do not start with a digit, and are used once.',
            'Click “Save” after adding, changing or removing one.',
            'Write {{ global.api_base }} where the value goes; typing {{ in a tool field or a prompt lists them.',
            'Values come back as plain text: keep secrets in a provider’s key field, not here.',
        ],
        media: { id: 'settings/variables', alt: 'Settings → Variables: AP_APPROVAL_LIMIT and SUPPORT_EMAIL, values tools and steps can read.' },
        guide: '/guides/start/setup',
    },
    memory: {
        title: 'Memory',
        summary: 'Clear stored data for good: chat history, run history and logs, agent memory, usage records, indexed repositories and saved database connections.',
        steps: [
            'Under “Data categories”, tick each kind of data to clear.',
            'Click “Clear selected”; it counts what you ticked.',
            'Check the list and click “Yes, clear selected”. This cannot be undone.',
            'Vault files and knowledge bases are not on the list, so nothing here clears them.',
        ],
        media: { id: 'settings/oss-memory', alt: 'Settings → Memory: the kinds of stored data, and clearing them.' },
        guide: '/guides/start/setup',
    },
    workspace: {
        title: 'Integrations',
        summary: 'Connect your Google account so agents can use Gmail, Drive, Calendar and other Google tools, and connect n8n so its workflows can become tools.',
        steps: [
            'With no credentials yet, follow the “Quick setup guide” to create a Google OAuth client and download its JSON.',
            'Paste the file’s contents into the credentials box; valid JSON saves by itself.',
            'Click “Connect Google account”, sign in on Google’s page, then come back and refresh the status.',
            'Under n8n, enter its URL and API key, and click “Save changes”.',
        ],
        media: { id: 'settings/oss-integrations', alt: 'Settings → Integrations: Google and n8n.' },
        guide: '/guides/start/tools',
    },
    messaging: {
        title: 'Channels',
        summary: 'Let people reach your agents from Telegram, Discord, Slack, Teams or WhatsApp. Each channel is one bot, answered by the agents you assign to it.',
        steps: [
            'Click “New channel”, pick a platform and name the channel.',
            'Fill in the platform’s credentials; its setup guide says where each one comes from.',
            'Click “Save & connect”, then pick the agents that answer it; the default one takes plain messages.',
            'Click “Test” to check it, and pick it under “Run notifications” to hear about workflow runs there.',
            'If messaging is off for this install, no channels load and saving fails.',
        ],
        media: { id: 'settings/oss-messaging', alt: 'Channels before any channel is set up: Telegram, Discord, Slack, Teams or WhatsApp, with run notifications and the webhook address; Widgets is a tab beside it.' },
        guide: '/guides/recipes/schedules-and-channels',
    },
    phone: {
        title: 'Phone',
        summary: 'Let people phone one of your Twilio numbers and talk to an agent. Callers are visitors: they use only the tools you tick, and every call is kept with its transcript.',
        steps: [
            'Click “Add a number”, paste a Twilio Account SID and an API key (a Restricted key with the permissions listed, or a Standard key), and click “Check and continue”.',
            'Pick the number; it is pointed here, and put back where it was if you turn it off or delete it.',
            'Choose the agent that answers, the words callers hear first, a hand-off number and the language, then “Connect number”.',
            'Open a number’s recent calls to read each call’s transcript, its tools and what it cost.',
            'Twilio must reach this server’s backend directly: set a public https webhook address on the Channels tab, such as a tunnel to the backend port.',
        ],
        guide: '/guides/recipes/phone-calls',
    },
    widgets: {
        title: 'Widgets',
        summary: 'Put an agent on your own website with one script tag. Only the sites you allow can load it, and visitors can use only the tools you tick.',
        steps: [
            'Click “New widget”, name it, choose an agent, list the allowed sites, and click “Create”.',
            'Under “What visitors can use”, tick only the tools strangers may use; none are ticked to start.',
            'Set the header, greeting, colours and theme; the preview follows as you go.',
            'Click “Save changes” — nothing but a logo is kept until you do.',
            'Click “Copy snippet” and paste the script tag into the HTML of each allowed site.',
        ],
        media: { id: 'settings/oss-widgets', alt: 'The Widgets tab of Channels before any widget: put an agent on your own site with one script tag.' },
        guide: '/guides/recipes/support-bot-widget',
    },
    repos: {
        title: 'Repos',
        summary: 'Code repositories on this machine that agents can search. With code indexing on, each one is indexed for search by meaning.',
        steps: [
            'Click “Add repo”, then give it a name, its absolute path and a description of what the code is.',
            'Under “Advanced settings”, exclude patterns or limit file types if you need to.',
            'Click “Save repository”. With “Code repository indexing” on in General, it starts indexing.',
            'Hover a card to index it again, stop indexing or delete it; click it to edit.',
        ],
        media: { id: 'settings/oss-repos', alt: 'Settings → Repos: code repositories agents can work in.' },
        guide: '/guides/start/tools',
    },
    db_configs: {
        title: 'DB configs',
        summary: 'Database connections whose schemas agents get as context: each database’s tables and what it is for.',
        steps: [
            'Click “Add database”, name it, choose its type and paste the connection string.',
            'Describe what the database holds, and click “Save database”.',
            'Hover the card and refresh its schema to read its tables; saving alone does not test the connection.',
            'Click a card to edit it, or hover it to delete it.',
        ],
        media: { id: 'settings/oss-databases', alt: 'Settings → DB configs: databases your agents can query.' },
        guide: '/guides/start/tools',
    },
    api_keys: {
        title: 'API keys',
        summary: 'Keys let your own code, or AI tools like Claude Code and Cursor, use Synapse without opening this app.',
        steps: [
            'Name the key after what will use it, and click “Generate”.',
            'Click “Copy key” and keep it somewhere safe; it is never shown again.',
            'Send it as a Bearer token in the Authorization header on calls to /api/v1/*.',
            '“API reference” opens every endpoint with examples; to use Synapse from Claude, Codex or Cursor, open Settings → MCP server.',
            'Removing a key, from its row, is immediate.',
        ],
        media: { id: 'settings/oss-api-keys', alt: 'Settings → API keys: keys for calling this install from your own code.' },
        guide: '/guides/developers/public-api',
    },
    mcp_server: {
        title: 'MCP server',
        summary: 'Use Synapse from Claude, Codex, Cursor or VS Code: ask them to run your workflows, answer approvals and build agents, without opening this app.',
        steps: [
            'Pick the AI tool you use.',
            'Click “Make a key for …”, or paste a key you already have. A key made here is shown only on this page.',
            'Copy the setup and paste it where the page says: a terminal for Claude Code and Codex, a config file for the others.',
            'Ask the tool “List my Synapse workflows.” The page says “Connected” once its first call arrives.',
            'Remove the key in Settings → API keys whenever you want to cut the tool off. Home’s “Use Synapse from your AI tools” connects most tools in one click.',
        ],
        guide: '/guides/developers/mcp',
    },
    import_export: {
        title: 'Import / Export',
        summary: 'Move workflows, agents, apps and tools between Synapse installs as one JSON bundle, or start from a ready-made example.',
        steps: [
            'On “Export”, click “Load export data” and tick what to take; what those need is added too.',
            'Name the file and click “Export & download”.',
            'On “Import”, drop a bundle or click to browse, then untick anything you do not want.',
            'Click “Import Now”, or “Configure Secrets →” when servers or tools need secret values.',
            'On “Examples”, click “Preview & Import” on a pack to review it before importing.',
        ],
        media: { id: 'settings/oss-import-export', alt: 'Settings → Import / Export: moving workflows, agents, apps and tools in and out as a bundle.' },
        guide: '/guides/start/setup',
    },
    scale: {
        title: 'Scale',
        summary: 'Run Synapse across several processes: connect Redis, Postgres and S3-compatible storage, turn on scale mode, and watch runs, workers and failed jobs.',
        steps: [
            'Click “Deploy docs” for the deployment guide: what you need, Docker and Kubernetes, and a checklist.',
            'Under “Redis connection”, enter the Redis URL, click “Test”, and switch on “Enable scale mode”.',
            'Set Postgres and S3 storage in their sections, testing each.',
            'Click “Save scale config”, then restart the server and every worker.',
            'Search a run or session ID, and watch the queue, workers and failed jobs at the top.',
        ],
        media: { id: 'settings/oss-scale', alt: 'Settings → Scale: running with workers and a shared database.' },
        guide: '/guides/start/setup',
    },
    support: {
        title: 'Help & docs',
        summary: 'The documentation, a quick start to your first multi-agent workflow, answers to common questions, and the community.',
        steps: [
            'Click a card under “Documentation” to open that guide on docs.synapseorch.com.',
            'Work through the “Quick start guide”; each step’s link opens the page where you do it.',
            'Open a question under “Frequently asked questions” for a short answer.',
            'Click “Join Discord server” to ask the community.',
        ],
        media: { id: 'settings/oss-support', alt: 'Settings → Help & docs: where to read the docs and get help.' },
        guide: '/guides/start/how-it-fits',
    },
    datalab: {
        title: 'Data lab',
        summary: 'Synthetic training data: conversation pairs for a topic and persona, written by any model the workspace can reach, saved as JSONL datasets.',
        steps: [
            'Enter a domain or topic, and how many conversation pairs to make.',
            'Choose a model, or keep the workspace default.',
            'Describe the assistant in “Target persona (system prompt)” and add any edge cases and constraints.',
            'Click “Start generation job”; one job runs at a time, and its file appears under Datasets when done.',
        ],
        guide: '/guides/start/setup',
    },
};
