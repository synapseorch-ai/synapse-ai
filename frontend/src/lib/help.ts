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
            'Click “Build with AI” in the menu to build with AI from any page; on a phone, the mark in the top bar. Settings sits at the foot.',
        ],
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
        media: { id: 'home/oss-dashboard', alt: 'The Home page: a greeting, a run waiting for approval under Needs you, the week’s figures and runs, recent runs and what is coming up.' },
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
            'Choose “Agent Logs”, “Orchestration Logs” or “Schedule Logs” at the top.',
            'Use “Filter” to show one agent, workflow or schedule; it appears once the list holds more than one.',
            'Click a log on the left to read it on the right; a run another run started sits beneath it.',
            'Click “Load More” for older logs, or the refresh button for new ones.',
            'Deleting a log, from its row, is immediate and cannot be undone.',
        ],
        media: { id: 'runs/oss-runs-logs', alt: 'Runs & Logs: tabs for agent, orchestration and schedule logs, runs grouped by session on the left and a log on the right.' },
        guide: '/guides/start/runs',
    },
    evals: {
        title: 'Evals',
        summary: 'Rubrics say what a good run looks like. A model judges every run of a workflow bound to one and flags regressions; golden sets replay fixed inputs.',
        steps: [
            'Click “New” beside Rubrics, add criteria with “Add criterion”, set “Floor” and “Drop”, and click “Save rubric”.',
            'In Workflows, open one and choose the rubric under Guardrails → “Score runs against”, then save it.',
            'Pick the workflow in “Show scores for” to see its scores and flagged runs, and say whether you agree.',
            'If it says “Nothing scored yet”, click “Score earlier runs” to judge runs from before the rubric.',
            'Under Golden sets, click “New set”, add cases and “Save set”; “Run” tells you the cost before it starts.',
        ],
        media: { id: 'evals/rubric', alt: 'Evals: rubrics and golden sets on the left; an orchestration’s scores, trend line and scored runs on the right.' },
        guide: '/guides/start/evals',
    },
    usage: {
        title: 'Usage',
        summary: 'What your model calls have cost: totals, cache savings, cost by model and session, and the price table the estimates come from.',
        steps: [
            'Read “Total Cost”, “Total Requests”, “Input Tokens” and “Output Tokens”; costs are estimates from the price table.',
            'Check “Cache Analytics” for what caching saved, by model and by workflow run.',
            'Under “Session History”, pick a “Filter by” option and click a session for its turn-by-turn cost.',
            'In “Pricing Reference”, edit a rate or click “Add model”, then “Save Pricing” to match what you pay.',
            '“Clear Logs” deletes every usage record once you confirm; it cannot be undone.',
        ],
        media: { id: 'usage/overview', alt: 'Usage: stat cards for cost, requests and tokens, cache analytics, cost by model and the pricing table.' },
        guide: '/guides/start/runs',
    },

    // ── Build ───────────────────────────────────────────────────────────────
    orchestrations: {
        title: 'Workflows',
        summary: 'Multi-agent workflows, built on a canvas. This page lists them and their runs; open one to edit it, or start a new one.',
        steps: [
            'Click “New” for a blank workflow, or “Build with AI” to describe one to the AI builder.',
            'On the “Orchestrations” tab, click a row or “Open →” to edit it.',
            'Switch to “Active”, “Recent” or “All runs” for each run’s status, current step, cost and duration.',
            'Click a run or “View →” to open it with its log; “Needs input” means it is waiting for you.',
        ],
        media: { id: 'workflows/list', alt: 'Orchestrations: a toolbar with New and Build with AI above tabs for orchestrations and their runs, and a table of orchestrations.' },
        guide: '/guides/start/workflows',
    },
    'orchestrations.editor': {
        title: 'Editing a workflow',
        summary: 'The workflow fills the canvas and its tools float over it: the bar at the top, the setup card at the right, the dock at the bottom and the run strip at the bottom left.',
        steps: [
            'Press “Add step” in the dock (or ⌘K) and pick a step, or drag one onto the spot you want; drag between handles to connect steps.',
            'Click a step to set it up in the card on the right: its name, its agent or prompt, its output key and the next step. Esc or a click on empty canvas puts the card away.',
            'Open “Workflow settings” for the description, the run’s input, its schedules, the shared state keys and the turn and time limits.',
            'Click “Save” or press Ctrl+S; anything to fix shows on the step and under “to check” in the bar.',
            'Click “Run now” — it runs the last saved version. The run strip says where it is; “Run Log” opens what it did.',
        ],
        media: { id: 'workflows/add-step', clip: true, alt: 'A step dragged from the palette onto the canvas, connected to the step before it and set up in the side panel.' },
        guide: '/guides/start/first-workflow',
    },
    agents: {
        title: 'Agents',
        summary: 'The agents that do the work: their instructions, model, tools and skills. An agent can be chatted with, scheduled, or used as a step in a workflow.',
        steps: [
            'Click “New agent” for a new one, or the arrow beside it and “Build Agent with AI…” to describe one and let AI build it. The first agent opens on its own.',
            'On “Setup”, set its name, description and model; “Use default” follows Settings → Models.',
            'Under “Instructions”, describe the agent in “AI Prompt Writer” and click “Generate”, or write the instructions yourself.',
            'Turn on what it may use under “Tools” (and its Tool Policy), then its skills and, when shown, knowledge bases.',
            'Click “Save” in the bar at the foot. Try it in the test chat on the right, which talks to the saved agent; “Runs”, “Metrics” and “Used by” show what it ran, what it cost and where it is used.',
        ],
        media: { id: 'agents/oss-editor', alt: 'Agents: the list of agents on the left, an agent’s setup in the middle, and a test chat on the right.' },
        guide: '/guides/start/agents',
    },
    skills: {
        title: 'Skills',
        summary: 'A skill is a named set of instructions. An agent sees each of its skills’ descriptions every turn, and reads the full instructions only when a task calls for them.',
        steps: [
            'Click “New skill”, then give it a lower-case name and a one-line description of when it applies.',
            'Write the instructions in Markdown, check them under “Preview”, and click “Save skill”.',
            'Edit a skill from its card; editing a “Disk” skill makes a “Library” copy that takes its place.',
            'In Agents, tick the skill under Skills on an agent’s “Configuration”, then click “Save Agent”.',
        ],
        media: { id: 'skills/list', alt: 'Skills: a grid of skill cards, each with its name, a Library or Disk badge and a description.' },
        guide: '/guides/start/agents',
    },
    chat: {
        title: 'Chat',
        summary: 'Talk to one agent at a time and watch it work: its thinking, the tools it calls and what they return. Conversations are kept for you to pick up again.',
        steps: [
            'Click the agent’s name at the foot of the message box and pick who to talk to; switching agents starts a new conversation.',
            'Type your message and press Enter; Shift+Enter adds a line, and “Stop” halts the agent.',
            'Attach, paste or drop images when the agent’s model can read them; a workflow agent takes them when one of its steps has “Sees attachments” on.',
            'Click + for a new chat, or the history button to reopen or delete an earlier one.',
        ],
        media: { id: 'chat/oss-home', alt: 'Chat: the agent’s name and model in the header, a conversation with a tool call opened, and the message box below.' },
        guide: '/guides/start/chat',
    },
    schedules: {
        title: 'Schedules',
        summary: 'Run an agent or a workflow on its own, every so often or at set times, with a standing prompt.',
        steps: [
            'Click “New Schedule”, name it, choose “Agent” or “Orchestration” under “Run as”, and pick which one.',
            'Write the “Prompt”: the instruction it is given every time the schedule fires.',
            'Choose “Interval” and how often, or “Fixed Time (Cron)” with a preset and an hour; times are in UTC.',
            'Leave it enabled and click “Create Schedule”. Each row can be paused, run now, edited or deleted.',
            'Each run’s log is in Runs › Logs, under “Schedule Logs”.',
        ],
        media: { id: 'schedules/list', alt: 'Schedules: a table of schedules with their target, timing, next and last run, and the schedule form beside it.' },
        guide: '/guides/recipes/schedules-and-channels',
    },

    templates: {
        title: 'Templates',
        summary: 'Ready-made packs of agents, workflows and MCP servers. You see everything a pack holds before anything is imported.',
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
        summary: 'HTTP, n8n and Python tools your agents can call. Each has a name, a description the model reads to decide when to use it, and a schema for its arguments.',
        steps: [
            'Click a tool in the list to edit it beside the list, or click “New Tool”, name it, and write a “Description (For AI)” saying when the model should use it.',
            'For HTTP, choose a method, enter the URL and list its arguments in “Input Schema (JSON)”.',
            'For n8n, connect it in Settings → Integrations first, then pick the workflow under “n8n Workflow”.',
            'For Python, write code that prints its result on the Python tab and try it with “Run”; it needs Docker.',
            'Click “Save”, then tick the tool under “Capabilities (Tools)” in Agents. “Import OpenAPI” makes tools from an API spec.',
        ],
        media: { id: 'tools/list', alt: 'Tools: custom tools in a list, each with an HTTP, n8n or Python badge, beside the open tool’s editor.' },
        guide: '/guides/start/tools',
    },
    mcp_servers: {
        title: 'Apps',
        summary: 'Connect Model Context Protocol servers, and their tools are offered to your agents. A local server is a command this machine runs; a remote one is reached by URL.',
        steps: [
            'Google Workspace is connected in Integrations; the card at the top goes there. For a server, choose “Local (stdio)” or “Remote (URL)”, and click a preset to fill in the form, or fill it in yourself.',
            'Enter a “Display Label” and a “Unique ID”; for a local server, its command, arguments and environment.',
            'For a remote server, enter the “Server URL”; leave the token empty to sign in with OAuth instead.',
            'Click “Connect Server”, then tick its tools under “Capabilities (Tools)” in Agents.',
            'If this install is reachable over a network, turn local servers off or limit their commands.',
        ],
        media: { id: 'mcp/browse', alt: 'MCP Servers: connected servers with their type and status, above the form to add one with its presets.' },
        guide: '/guides/start/tools',
    },
    vault: {
        title: 'Vault',
        summary: 'Files your agents can use when a prompt names them with @[path]. Write Markdown, JSON and text files here, in folders.',
        steps: [
            'Use the new folder and new file buttons; a file is Markdown, JSON or plain text.',
            'Select a file to edit it, and click “Save” while it shows unsaved changes.',
            'In an agent’s system prompt or a step’s prompt, type @ and pick a file; it goes in as @[path].',
            'With Knowledge on, a folder that is a knowledge base shows a “Knowledge” badge; make one in Knowledge.',
        ],
        media: { id: 'vault/browse', alt: 'The Vault: a folder tree on the left and a Markdown file open in the editor on the right.' },
        guide: '/guides/start/tools',
    },
    knowledge: {
        title: 'Knowledge',
        summary: 'Knowledge makes Vault folders searchable, so agents answer from your documents and cite them. Each knowledge base is one Vault folder.',
        steps: [
            'If it says Knowledge is off, turn on “Knowledge” in Settings → General and click “Save Changes”.',
            'In Settings → Models, choose an embedding model for knowledge; without one, search matches words only.',
            'Click “New knowledge base”, name it, and pick a Vault folder or type a new one.',
            'Choose a “Preset” — “Cheap”, “Balanced” or “High recall” — and click “Create”.',
            'Files already in the folder start indexing. Select the base to add more and follow each file.',
        ],
        media: { id: 'knowledge/bases', alt: 'Knowledge: the list of knowledge bases on the left and the New knowledge base button.' },
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
        media: { id: 'knowledge/upload', clip: true, alt: 'Files dropped onto a knowledge base, each moving from Queued through Reading and Indexing to Indexed.' },
        guide: '/guides/knowledge/sources',
    },

    // ── Settings ────────────────────────────────────────────────────────────
    general: {
        title: 'General',
        summary: 'How the whole workspace behaves: the agent’s name, Knowledge, long outputs and histories, tool policy, database writes, sandboxing and sign-in.',
        steps: [
            'Set the “Global Agent Name”, and turn on “Knowledge” to make Vault folders searchable.',
            'Use “Large Response Handling” and “Auto Context Compaction” to keep long outputs and histories in bounds.',
            'Under “Tool Policy”, choose “Allow”, “Ask first” or “Block” for each kind of tool.',
            'Keep “Database Write Access” off unless agents must write, and list the folders the bash tool may use.',
            'Click “Save Changes”. To require a password, turn on “Require Login” and click “Save Login Settings”.',
        ],
        media: { id: 'settings/oss-general', alt: 'Settings → General: the agent name field and the switches for Knowledge, large responses and context compaction.' },
        guide: '/guides/start/setup',
    },
    models: {
        title: 'Models',
        summary: 'Connect model providers and choose the models the workspace uses. Agents with no model of their own use the default.',
        steps: [
            'Under “Providers”, open one and paste its key; the link inside says where to get one.',
            'A provider with a model count is ready; “No key configured”, “Not running” or “Not installed” says what it needs.',
            'Choose the “Default Model”, and a “Builder Model” if the AI builder should use another.',
            'Set “Reasoning Effort” and “Max Response Length” for agents that set neither.',
            'Choose an “Embedding Model” and, with Knowledge on, its models; then click “Save Changes”.',
        ],
        media: { id: 'settings/oss-models', alt: 'Settings → Models: provider cards, one open to its API key field, above the default model picker.' },
        guide: '/guides/start/setup',
    },
    personal_details: {
        title: 'Personal Details',
        summary: 'Your name, contact details and address, kept so an agent can fill in a form or a billing address for you when a task needs it.',
        steps: [
            'Fill in your name, email and phone number, and the address if tasks need one.',
            'Leave a field empty if you would rather agents did not have it, and click “Save Changes”.',
            'In Agents, tick “Personal Details” under “Capabilities (Tools)” for each agent that should read them.',
        ],
        media: { id: 'settings/oss-profile', alt: 'Settings → Personal Details: name, email and phone fields above the address.' },
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
        media: { id: 'settings/variables', alt: 'Settings → Variables: name and value rows, with Add variable and Save below.' },
        guide: '/guides/start/setup',
    },
    memory: {
        title: 'Memory',
        summary: 'Clear stored data for good: chat history, run history and logs, agent memory, usage records, indexed repositories and saved database connections.',
        steps: [
            'Under “Data Categories”, tick each kind of data to clear.',
            'Click “Clear Selected”; it counts what you ticked.',
            'Check the list and click “Yes, Clear Selected”. This cannot be undone.',
            'Vault files and knowledge bases are not on the list, so nothing here clears them.',
        ],
        media: { id: 'settings/oss-memory', alt: 'Settings → Memory: the data categories as cards to tick, and the Clear Selected button below.' },
        guide: '/guides/start/setup',
    },
    workspace: {
        title: 'Integrations',
        summary: 'Connect your Google account so agents can use Gmail, Drive, Calendar and other Google tools, and connect n8n so its workflows can become tools.',
        steps: [
            'With no credentials yet, follow the “Quick Setup Guide” to create a Google OAuth client and download its JSON.',
            'Paste the file’s contents into the credentials box; valid JSON saves by itself.',
            'Click “Connect Google Account”, sign in on Google’s page, then come back and refresh the status.',
            'Under n8n, enter its URL and API key, and click “Save Changes”.',
        ],
        media: { id: 'settings/oss-integrations', alt: 'Settings → Integrations: the Google Workspace card with its connection status, above the n8n card.' },
        guide: '/guides/start/tools',
    },
    messaging: {
        title: 'Channels',
        summary: 'Let people reach your agents from Telegram, Discord, Slack, Teams or WhatsApp. Each channel is one bot, answered by the agents you assign to it.',
        steps: [
            'Click + beside Channels, pick a platform and name the channel.',
            'Fill in the platform’s credentials; its setup guide says where each one comes from.',
            'Click “Save & Connect”, then pick the agents that answer it; the default one takes plain messages.',
            'Click “Test” to check it, and pick it under “Run notifications” to hear about workflow runs there.',
            'If messaging is off for this install, no channels load and saving fails.',
        ],
        media: { id: 'settings/oss-messaging', alt: 'Settings → Messaging: channels and their status on the left, and a channel’s platform, agents and credentials on the right.' },
        guide: '/guides/recipes/schedules-and-channels',
    },
    widgets: {
        title: 'Widgets',
        summary: 'Put an agent on your own website with one script tag. Only the sites you allow can load it, and visitors can use only the tools you tick.',
        steps: [
            'Click “New widget”, name it, choose an agent, list the allowed sites, and click “Create”.',
            'Under “What visitors can use”, tick only the tools strangers may use; none are ticked to start.',
            'Set the header, greeting, colours and theme; the preview follows as you go.',
            'Click “Save changes” — nothing but a logo is kept until you do.',
            'Copy the script tag and paste it into the HTML of each allowed site.',
        ],
        media: { id: 'settings/oss-widgets', alt: 'Settings → Widgets: a widget’s script tag, allowed sites, visitor tools and branding, beside a live preview.' },
        guide: '/guides/recipes/support-bot-widget',
    },
    repos: {
        title: 'Repos',
        summary: 'Code repositories on this machine that agents can search. With code indexing on, each one is indexed for search by meaning.',
        steps: [
            'Click “Add Repo”, then give it a name, its absolute path and a description of what the code is.',
            'Under “Advanced settings”, exclude patterns or limit file types if you need to.',
            'Click “Save Repository”. With “Code Repository Indexing” on in General, it starts indexing.',
            'Hover a card to index it again, stop indexing or delete it; click it to edit.',
        ],
        media: { id: 'settings/oss-repos', alt: 'Settings → Repos: repositories as cards, each with its path, an indexing badge and a count of indexed chunks.' },
        guide: '/guides/start/tools',
    },
    db_configs: {
        title: 'DB Configs',
        summary: 'Database connections whose schemas agents get as context: each database’s tables and what it is for.',
        steps: [
            'Click “Add Database”, name it, choose its type and paste the connection string.',
            'Describe what the database holds, and click “Save Database”.',
            'Hover the card and refresh its schema to read its tables; saving alone does not test the connection.',
            'Click a card to edit it, or hover it to delete it.',
        ],
        media: { id: 'settings/oss-databases', alt: 'Settings → DB Configs: database connections as cards, with a status badge, type, masked connection string and cached tables.' },
        guide: '/guides/start/tools',
    },
    api_keys: {
        title: 'API Keys',
        summary: 'Keys that let your scripts, servers and MCP clients such as Claude Code or Cursor drive Synapse from outside this UI.',
        steps: [
            'Name the key after what will use it, and click “Generate”.',
            'Click “Copy key” and keep it somewhere safe; it is never shown again.',
            'Send it as a Bearer token in the Authorization header on calls to /api/v1/*.',
            'Click “View Docs” for the API reference: endpoints, examples, and MCP setup for Claude Code or Cursor.',
            'Removing a key, from its row, is immediate.',
        ],
        media: { id: 'settings/oss-api-keys', alt: 'Settings → API Keys: a field to name and generate a key, and the active keys with their prefix and last use.' },
        guide: '/guides/developers/public-api',
    },
    import_export: {
        title: 'Import / Export',
        summary: 'Move workflows, agents, MCP servers and tools between Synapse installs as one JSON bundle, or start from a ready-made example.',
        steps: [
            'On “Export”, click “Load Export Data” and tick what to take; what those need is added too.',
            'Name the file and click “Export & Download”.',
            'On “Import”, drop a bundle or click to browse, then untick anything you do not want.',
            'Click “Import Now”, or “Configure Secrets →” when servers or tools need secret values.',
            'On “Examples”, click “Preview & Import” on a pack to review it before importing.',
        ],
        media: { id: 'settings/oss-import-export', alt: 'Settings → Import / Export: orchestrations, agents, servers and tools as checkboxes to export.' },
        guide: '/guides/start/setup',
    },
    scale: {
        title: 'Scale',
        summary: 'Run Synapse across several processes: connect Redis, Postgres and S3-compatible storage, turn on scale mode, and watch runs, workers and failed jobs.',
        steps: [
            'Click “Deploy Docs” for the deployment guide: what you need, Docker and Kubernetes, and a checklist.',
            'Under “Redis Connection”, enter the Redis URL, click “Test”, and switch on “Enable Scale Mode”.',
            'Set Postgres and S3 storage in their sections, testing each.',
            'Click “Save Scale Config”, then restart the server and every worker.',
            'Search a run or session ID, and watch the queue, workers and failed jobs at the top.',
        ],
        media: { id: 'settings/oss-scale', alt: 'Settings → Scale: run analytics, recent runs, the live queue and workers, above the Redis, Postgres and storage sections.' },
        guide: '/guides/start/setup',
    },
    support: {
        title: 'Help & Docs',
        summary: 'The documentation, a quick start to your first multi-agent workflow, answers to common questions, and the community.',
        steps: [
            'Click a card under “Documentation” to open that guide on docs.synapseorch.com.',
            'Work through the “Quick Start Guide”; each step’s link opens the page where you do it.',
            'Open a question under “Frequently Asked Questions” for a short answer.',
            'Click “Join Discord Server” to ask the community.',
        ],
        media: { id: 'settings/oss-support', alt: 'Settings → Help & Docs: documentation links, a quick start guide and frequently asked questions.' },
        guide: '/guides/start/how-it-fits',
    },
    datalab: {
        title: 'Data Lab',
        summary: 'Synthetic training data: conversation pairs for a topic and persona, written by any model the workspace can reach, saved as JSONL datasets.',
        steps: [
            'Enter a domain or topic, and how many conversation pairs to make.',
            'Choose a model, or keep the workspace default.',
            'Describe the assistant in “Target Persona (System Prompt)” and add any edge cases and constraints.',
            'Click “Start Generation Job”; one job runs at a time, and its file appears under Datasets when done.',
        ],
        guide: '/guides/start/setup',
    },
};
