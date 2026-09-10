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
    Braces,
    ArrowLeftRight, BookOpen, Bot, Clock, Cloud, Cpu, Database, DollarSign, Eraser, LayoutDashboard,
    FolderGit2, Gauge, Globe, Key, LayoutGrid, LifeBuoy, MessageSquare, MessagesSquare,
    ScrollText, Server, Shield, Vault, Workflow, Wrench,
    type LucideIcon,
} from 'lucide-react';

/** A settings flag from GET /api/settings that gates a nav entry. */
export type NavFlag = 'messaging_enabled' | 'coding_agent_enabled';

export type NavEntry = {
    /** Legacy settings tab id, where one exists — also the SettingsView `initialTab`. */
    id: string;
    label: string;
    href: string;
    /** One line, shown under the title by <Screen>. Never a template. */
    blurb: string;
    icon: LucideIcon;
    /** Renders the unseen-run count. Only Orchestrations uses it. */
    badge?: 'unseenRuns';
    /** Hidden unless this /api/settings flag is on. */
    flag?: NavFlag;
};

export type NavGroup = { group: string | null; items: NavEntry[] };

/**
 * The rail. Three groups, read top to bottom as *watch → author → connect*:
 * Overview is the workspace as it stands and what it has done; Build is what
 * you author; Tools is what agents reach through. Settings is rendered
 * separately at the foot.
 */
export const PRIMARY_NAV: NavGroup[] = [
    {
        group: 'Overview',
        items: [
            {
                id: 'dashboard', label: 'Home', href: '/dashboard', icon: LayoutDashboard,
                blurb: 'Where the workspace stands right now.',
            },
            {
                id: 'logs', label: 'Runs & Logs', href: '/runs', icon: ScrollText,
                blurb: 'Replay what agents, orchestrations and schedules actually did.',
            },
            {
                id: 'usage', label: 'Usage', href: '/usage', icon: DollarSign,
                blurb: 'Token usage and cost tracking across every LLM call.',
            },
        ],
    },
    {
        group: 'Build',
        items: [
            {
                id: 'orchestrations', label: 'Orchestrations', href: '/orchestrations', icon: Workflow,
                blurb: 'Design multi-agent workflows on a visual canvas.',
                badge: 'unseenRuns',
            },
            {
                id: 'agents', label: 'Agents', href: '/agents', icon: Bot,
                blurb: 'Build and configure the agents that do the work.',
            },
            {
                id: 'skills', label: 'Skills', href: '/skills', icon: BookOpen,
                blurb: 'Reusable instruction packages agents load on demand.',
            },
            {
                id: 'chat', label: 'Chat', href: '/', icon: MessagesSquare,
                blurb: 'Talk to an agent and watch it work.',
            },
            {
                id: 'schedules', label: 'Schedules', href: '/schedules', icon: Clock,
                blurb: 'Automate agent and orchestration runs on a recurring schedule.',
            },
        ],
    },
    {
        group: 'Tools',
        items: [
            {
                id: 'custom_tools', label: 'Tools', href: '/tools', icon: Wrench,
                blurb: 'Wire up HTTP, Python and n8n tools your agents can call.',
            },
            {
                id: 'mcp_servers', label: 'MCP Servers', href: '/mcp-servers', icon: Server,
                blurb: 'Connect Model Context Protocol servers and their tool catalogues.',
            },
            {
                id: 'vault', label: 'Vault', href: '/vault', icon: Vault,
                blurb: 'Knowledge files and skills agents reference with @[path] in their prompts.',
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
                blurb: 'Pick a provider and model, and hold their API keys.',
            },
            {
                id: 'personal_details', label: 'Personal Details', href: '/settings/personal_details', icon: Shield,
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
                id: 'messaging', label: 'Messaging', href: '/settings/messaging', icon: MessageSquare,
                blurb: 'Reach your agents from Telegram, Discord, Slack, Teams or WhatsApp.',
                flag: 'messaging_enabled',
            },
            {
                id: 'widgets', label: 'Widgets', href: '/settings/widgets', icon: Globe,
                blurb: 'Embed an agent in your own site with one script tag.',
            },
            {
                id: 'repos', label: 'Repos', href: '/settings/repos', icon: FolderGit2,
                blurb: 'Index code repositories so agents can search them.',
                flag: 'coding_agent_enabled',
            },
            {
                id: 'db_configs', label: 'DB Configs', href: '/settings/db_configs', icon: Database,
                blurb: 'Database connections your agents are allowed to query.',
                flag: 'coding_agent_enabled',
            },
        ],
    },
    {
        group: 'Access & data',
        items: [
            {
                id: 'api_keys', label: 'API Keys', href: '/settings/api_keys', icon: Key,
                blurb: 'Issue keys for driving Synapse from outside this UI.',
            },
            {
                id: 'import_export', label: 'Import / Export', href: '/settings/import_export', icon: ArrowLeftRight,
                blurb: 'Move orchestrations, agents, servers and tools between instances as a bundle.',
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
                id: 'support', label: 'Help & Docs', href: '/settings/support', icon: LifeBuoy,
                blurb: 'Documentation, FAQs, and where to get help.',
            },
        ],
    },
];

// Re-exported from a dependency-free module so next.config.ts can import the
// same list without pulling React in. See src/lib/moved-routes.ts.
export { MOVED_TABS } from './moved-routes';

/**
 * Tabs SettingsView still renders that deliberately have no nav entry. `datalab`
 * was absent from both of the old tab lists too — reachable only by typing the
 * URL. Listed here so the settings route lets it through instead of treating it
 * as a typo.
 */
export const UNLISTED_TABS: readonly string[] = ['datalab'];

const ALL_ENTRIES: NavEntry[] = [...PRIMARY_NAV, ...SETTINGS_NAV].flatMap(g => g.items);

/** Look an entry up by its tab id (`api_keys`) or by its href (`/settings/api_keys`). */
export function navEntryFor(idOrHref: string): NavEntry | undefined {
    return ALL_ENTRIES.find(e => e.id === idOrHref || e.href === idOrHref);
}

/** True for tabs that live under the Settings section (so they get a close control). */
export function isSettingsEntry(id: string): boolean {
    return SETTINGS_NAV.some(g => g.items.some(i => i.id === id));
}

/** Drop entries whose flag is off. `flags` is the /api/settings payload subset. */
export function visibleItems(items: NavEntry[], flags: Partial<Record<NavFlag, boolean>>): NavEntry[] {
    return items.filter(e => !e.flag || flags[e.flag]);
}
