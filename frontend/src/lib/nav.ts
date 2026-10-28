/*
 * The single source of truth for both levels of navigation.
 *
 * Before this module the tab list existed twice — in app/settings/layout.tsx and
 * again in SettingsView.tsx — so a rename landed in one and not the other. It
 * also carries the per-screen `blurb`, which replaces the template that produced
 * "Manage your agent's api_keys configuration." for twenty different screens.
 *
 * NO "use client" DIRECTIVE. This module is imported by the client rail *and*
 * called by the server settings page. Next compiles every export of a "use
 * client" module into a client reference, so calling navEntryFor() from a server
 * component would fail — and the symptom is a 200 with a 404 body.
 */
import {
    Activity, ArrowLeftRight, Blocks, BookOpen, Bot, Braces, Cable, ClipboardList, Clock, Cloud, Cpu, Database, DollarSign, Eraser,
    FlaskConical, FolderGit2, FolderOpen, Gauge, Globe, Key, LayoutDashboard, LayoutGrid, LayoutTemplate, LifeBuoy, Library,
    MessageSquare, MessagesSquare, Phone, Radio, ScrollText, Shield, Target, Workflow, Wrench,
    type LucideIcon,
} from 'lucide-react';

/** A settings flag from GET /api/settings that gates a nav entry. */
export type NavFlag = 'messaging_enabled' | 'coding_agent_enabled' | 'knowledge_enabled' | 'boards_enabled';

export type NavEntry = {
    /** Legacy settings tab id, where one exists — also the SettingsView `initialTab`, and the help topic's key. */
    id: string;
    label: string;
    href: string;
    /**
     * Reachable by URL, but not offered in the rail. An entry is still a real
     * entry: it carries its own title and blurb, so the screen it names does
     * not borrow another screen's header (F45).
     */
    hidden?: boolean;
    /** One line, shown under the title by <Screen>. Never a template. */
    blurb: string;
    icon: LucideIcon;
    /** Hidden unless this /api/settings flag is on. */
    flag?: NavFlag;
};

export type NavGroup = { group: string | null; items: NavEntry[] };

/**
 * One item in the rail (D209). Its pages are its tabs, in order: each keeps its
 * own address, renders under the hub's one title with a tab bar, and lights the
 * hub in the rail. A hub with one page has no tab bar.
 */
export type NavHub = {
    id: string;
    label: string;
    icon: LucideIcon;
    tabs: NavEntry[];
    /** Renders the unseen-run count. Only Workflows uses it. */
    badge?: 'unseenRuns';
};

export type NavHubGroup = { group: string | null; hubs: NavHub[] };

/**
 * The rail: D209's hubs, the same in both products. Home and Runs watch, Build
 * is what you author, Connect is what agents reach through and where they meet
 * people. The Vault stands on its own (Naveen, M1b): agents, Knowledge and
 * workflows all use it. Settings is rendered separately at the foot.
 */
export const HUB_NAV: NavHubGroup[] = [
    {
        group: null,
        hubs: [
            {
                id: 'home', label: 'Home', icon: LayoutDashboard,
                tabs: [{
                    id: 'dashboard', label: 'Home', href: '/dashboard', icon: LayoutDashboard,
                    blurb: 'Where the workspace stands right now.',
                }],
            },
            {
                id: 'runs', label: 'Runs', icon: Activity,
                tabs: [
                    {
                        id: 'runs', label: 'Runs', href: '/runs', icon: Activity,
                        blurb: 'Every run of a workflow, newest first.',
                    },
                    {
                        id: 'logs', label: 'Logs', href: '/logs', icon: ScrollText,
                        blurb: 'Replay what agents, workflows and schedules actually did.',
                    },
                    {
                        id: 'evals', label: 'Evals', href: '/evals', icon: Target,
                        blurb: 'Check that your workflows keep doing a good job. Say what a good result looks like, and every run gets a score.',
                    },
                    {
                        id: 'usage', label: 'Usage', href: '/usage', icon: DollarSign,
                        blurb: 'What your AI models have cost, by model, by run and by chat.',
                    },
                ],
            },
        ],
    },
    {
        group: 'Build',
        hubs: [
            {
                id: 'workflows', label: 'Workflows', icon: Workflow, badge: 'unseenRuns',
                tabs: [
                    {
                        id: 'orchestrations', label: 'Workflows', href: '/workflows', icon: Workflow,
                        blurb: 'Design multi-agent workflows on a visual canvas.',
                    },
                    {
                        id: 'schedules', label: 'Schedules', href: '/schedules', icon: Clock,
                        blurb: 'Automate agent and workflow runs on a recurring schedule.',
                    },
                    {
                        id: 'templates', label: 'Templates', href: '/templates', icon: LayoutTemplate,
                        blurb: 'Ready-made packs of agents, workflows and servers to preview and import.',
                    },
                ],
            },
            {
                id: 'agents', label: 'Agents', icon: Bot,
                tabs: [
                    {
                        id: 'agents', label: 'Agents', href: '/agents', icon: Bot,
                        blurb: 'Build and configure the agents that do the work.',
                    },
                    {
                        id: 'skills', label: 'Skills', href: '/skills', icon: BookOpen,
                        blurb: 'Reusable instruction packages agents load on demand.',
                    },
                    {
                        // The browser inside Synapse (BRW2, F1139; DESIGN v5-10, Naveen 10-29: under Agents).
                        id: 'browser', label: 'Browser', href: '/browser', icon: Globe,
                        blurb: 'Sites your agents stay signed in to, and tasks recorded for them to repeat.',
                    },
                ],
            },
            {
                // Its own place, not a tab of Agents (Naveen, M1b): agents read the
                // Vault, workflows write their outputs to it, and every knowledge
                // base is one of its folders. An open folder, not lucide's `Vault` (a
                // crossed box at 16px): the workspace's files.
                id: 'vault', label: 'Vault', icon: FolderOpen,
                tabs: [{
                    id: 'vault', label: 'Vault', href: '/vault', icon: FolderOpen,
                    blurb: 'Knowledge files and skills agents reference with @[path] in their prompts.',
                }],
            },
            {
                id: 'knowledge', label: 'Knowledge', icon: Library,
                tabs: [{
                    id: 'knowledge', label: 'Knowledge', href: '/knowledge', icon: Library,
                    blurb: 'Let agents answer from your own documents, and show where each answer came from.',
                    flag: 'knowledge_enabled',
                }, {
                    // Beside Knowledge, not the Vault, which stands on its own (C7, BOARD1b).
                    id: 'boards', label: 'Boards', href: '/boards', icon: ClipboardList,
                    blurb: 'Shared work agents read, claim and close: each workflow\'s, each schedule\'s and shared boards.',
                    flag: 'boards_enabled',
                }],
            },
            {
                id: 'chat', label: 'Chat', icon: MessagesSquare,
                tabs: [{
                    id: 'chat', label: 'Chat', href: '/', icon: MessagesSquare,
                    blurb: 'Talk to an agent and watch it work.',
                }],
            },
        ],
    },
    {
        group: 'Connect',
        hubs: [
            {
                id: 'apps', label: 'Apps', icon: Blocks,
                tabs: [
                    {
                        id: 'mcp_servers', label: 'Apps', href: '/apps', icon: Blocks,
                        blurb: 'Connect the apps your agents work with, like GitHub, Notion or Figma. Each app\'s actions become tools your agents can use.',
                    },
                    {
                        id: 'custom_tools', label: 'Tools', href: '/tools', icon: Wrench,
                        blurb: 'Let your agents use other systems: call an API, run a bit of Python, or start an n8n workflow.',
                    },
                ],
            },
            {
                id: 'channels', label: 'Channels', icon: Radio,
                tabs: [
                    {
                        id: 'messaging', label: 'Channels', href: '/channels', icon: MessageSquare,
                        blurb: 'Reach your agents from Telegram, Discord, Slack, Teams or WhatsApp.',
                        flag: 'messaging_enabled',
                    },
                    {
                        // V29 (Naveen): numbers get their own tab; the Channels tab lists none (VOX1c).
                        id: 'phone', label: 'Phone', href: '/channels/phone', icon: Phone,
                        blurb: 'Let callers talk to an agent on one of your Twilio numbers.',
                        flag: 'messaging_enabled',
                    },
                    {
                        id: 'widgets', label: 'Widgets', href: '/widgets', icon: Globe,
                        blurb: 'Embed an agent in your own site with one script tag.',
                    },
                ],
            },
        ],
    },
];

/** The Settings second level. Grouped, so twenty tabs stop being one flat list. */
export const SETTINGS_NAV: NavGroup[] = [
    {
        group: 'Workspace',
        items: [
            {
                id: 'general', label: 'General', href: '/settings/general', icon: LayoutGrid,
                blurb: 'Agent identity, context limits, sandboxing and sign-in.',
            },
            {
                id: 'models', label: 'Models', href: '/settings/models', icon: Cpu,
                blurb: 'Add your own model keys, and choose the models agents use.',
            },
            {
                id: 'personal_details', label: 'Personal details', href: '/settings/personal_details', icon: Shield,
                blurb: 'Details agents may use when acting on your behalf.',
            },
            {
                id: 'variables', label: 'Variables', href: '/settings/variables', icon: Braces,
                blurb: 'Values used across tools and prompts as {{ global.key }}.',
            },
            {
                id: 'memory', label: 'Memory', href: '/settings/memory', icon: Eraser,
                blurb: 'Clear stored conversations, embeddings and run history.',
            },
        ],
    },
    {
        group: 'Connections',
        items: [
            {
                id: 'workspace', label: 'Integrations', href: '/settings/workspace', icon: Cloud,
                blurb: 'Connect n8n and your Google Workspace credentials.',
            },
            {
                id: 'repos', label: 'Repos', href: '/settings/repos', icon: FolderGit2,
                blurb: 'Index code repositories so agents can search them.',
                flag: 'coding_agent_enabled',
            },
            {
                id: 'db_configs', label: 'DB configs', href: '/settings/db_configs', icon: Database,
                blurb: 'Database connections your agents are allowed to query.',
                flag: 'coding_agent_enabled',
            },
        ],
    },
    {
        group: 'Access & data',
        items: [
            {
                id: 'api_keys', label: 'API keys', href: '/settings/api_keys', icon: Key,
                blurb: 'Keys that let your own code, or AI tools like Claude and Cursor, use Synapse.',
            },
            {
                // Every AI tool step by step (GUIDE1, F1027); Home's card connects most in one click and links here.
                // Naveen: "I don't want new ai tools menu ... a button that takes to an MCP server page in settings".
                id: 'mcp_server', label: 'MCP server', href: '/settings/mcp_server', icon: Cable,
                blurb: 'Use Synapse from Claude, Codex, Cursor or VS Code: run workflows, answer approvals and build agents without opening this app.',
            },
            {
                id: 'import_export', label: 'Import / Export', href: '/settings/import_export', icon: ArrowLeftRight,
                blurb: 'Move workflows, agents, servers and tools between instances as a bundle.',
            },
        ],
    },
    {
        group: 'Advanced',
        items: [
            {
                id: 'scale', label: 'Scale', href: '/settings/scale', icon: Gauge,
                blurb: 'Redis, Postgres and object storage for running across processes.',
            },
            {
                id: 'support', label: 'Help & docs', href: '/settings/support', icon: LifeBuoy,
                blurb: 'Documentation, FAQs, and where to get help.',
            },
            {
                id: 'datalab', label: 'Data lab', href: '/settings/datalab', icon: FlaskConical,
                hidden: true,
                blurb: 'Generate synthetic training datasets from a topic and a system prompt.',
            },
        ],
    },
];

// Re-exported from a dependency-free module so next.config.ts can import the
// same list without pulling React in. See src/lib/moved-routes.ts.
export { MOVED_ROUTES, MOVED_TABS } from './moved-routes';

const HUBS: NavHub[] = HUB_NAV.flatMap(g => g.hubs);
const ALL_ENTRIES: NavEntry[] = [...HUBS.flatMap(h => h.tabs), ...SETTINGS_NAV.flatMap(g => g.items)];

/** Look an entry up by its tab id (`api_keys`) or by its href (`/settings/api_keys`). */
export function navEntryFor(idOrHref: string): NavEntry | undefined {
    return ALL_ENTRIES.find(e => e.id === idOrHref || e.href === idOrHref);
}

/** True for tabs that live under the Settings section (so they get a close control). */
export function isSettingsEntry(id: string): boolean {
    return SETTINGS_NAV.some(g => g.items.some(i => i.id === id));
}

/**
 * Drop entries whose flag is off, and entries that are reachable only by URL.
 * `flags` is the /api/settings payload subset.
 */
export function visibleItems(items: NavEntry[], flags: Partial<Record<NavFlag, boolean>>): NavEntry[] {
    return items.filter(e => !e.hidden && (!e.flag || flags[e.flag]));
}

/** True when `pathname` is this page or below it. Chat (`/`) matches only itself. */
export function isAtHref(pathname: string, href: string): boolean {
    if (href === '/') return pathname === '/';
    return pathname === href || pathname.startsWith(`${href}/`);
}

/** The hub an entry (by id or href) belongs to, with the rail group it sits under. */
export function hubOf(idOrHref: string): { hub: NavHub; group: string | null; entry: NavEntry } | undefined {
    for (const { group, hubs } of HUB_NAV) {
        for (const hub of hubs) {
            const entry = hub.tabs.find(e => e.id === idOrHref || e.href === idOrHref);
            if (entry) return { hub, group, entry };
        }
    }
    return undefined;
}

/** The hub whose tabs hold this path, and the tab. */
export function hubForPath(pathname: string): { hub: NavHub; group: string | null; entry: NavEntry } | undefined {
    for (const { group, hubs } of HUB_NAV) {
        for (const hub of hubs) {
            const entry = hub.tabs.find(e => isAtHref(pathname, e.href));
            if (entry) return { hub, group, entry };
        }
    }
    return undefined;
}

/** A hub's tabs that show with these flags; a hub with none is not offered. */
export function visibleTabs(hub: NavHub, flags: Partial<Record<NavFlag, boolean>>): NavEntry[] {
    return visibleItems(hub.tabs, flags);
}
