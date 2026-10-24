/*
 * The guided tours (GUIDE1, D318, F920): a few stops on a main page, each lighting up one part and saying what it
 * is for. Naveen: "a highlighter will highlight a section and explain what we can do with this ... and they just
 * click next". Keyed by the page's help topic id (`lib/helpHost.tsx`). Chat's stops are TABS1's anchors (D319). A stop whose part isn't on the page right
 * now (a new workspace's empty state, a phone's hidden rail) is skipped, so one list serves every state.
 *
 * Every anchor must exist in the source (`test_every_tour_step_has_its_anchor.py`): a screen that drops one fails a
 * test instead of quietly losing its stop. Raise a tour's `version` when its words change enough to show it again.
 */
import type { Tour } from '@/components/help';
import { HUB_NAV } from '@/lib/nav';

export const TOURS: Record<string, Tour> = {
    dashboard: {
        version: 1,
        steps: [
            { anchor: 'home-ask', title: 'Ask Synapse AI', body: 'Ask about your runs in plain words, or describe something to build. It answers from what your workspace has done.' },
            { anchor: 'home-start', title: 'Your first steps', body: 'A short path from connecting a model to a workflow that runs on its own. Each step says what to do next.' },
            { anchor: 'home-needs-you', title: 'What needs you', body: 'When a workflow stops to ask a question or wait for your approval, it shows here. Answer it without leaving Home.' },
            { anchor: 'home-more', title: 'More you can do', body: 'Parts of Synapse this workspace hasn’t tried yet, such as scoring runs or answering from your documents.' },
            { anchor: 'rail-runs', title: 'Runs, scores and costs', body: 'Every run, its log, its score (Evals) and what it cost (Usage) live under Runs.' },
            { anchor: 'home-connect', title: 'Use it from your AI tools', body: 'Connect Claude, Codex, Cursor or VS Code in a click, and run your workflows from there.' },
            { anchor: 'help', title: 'Help on every page', body: 'The “?” explains the page you are on. You can take this tour again from there.' },
        ],
    },
    orchestrations: {
        version: 1,
        steps: [
            { anchor: 'workflows-build', title: 'Build with AI', body: 'Describe what you want done. Synapse AI drafts the workflow and its agents, and asks before it saves anything.' },
            { anchor: 'workflows-new', title: 'Or start from scratch', body: 'A blank canvas: add steps, connect them, and run it to see each step’s result.' },
            { anchor: 'workflows-templates', title: 'Ready-made workflows', body: 'Start from a template and make it yours. Each brings its agents and connections along.' },
            { anchor: 'workflows-views', title: 'Your workflows and their runs', body: 'Switch between your workflows and their latest runs. Open one to edit it on the canvas.' },
        ],
    },
    chat: {
        version: 1,
        steps: [
            { anchor: 'chat-tabs', title: 'Conversations as tabs', body: 'Each conversation is a tab, so several can work at once. A spinner means its agent is working; an amber dot means it needs you.' },
            { anchor: 'chat-tab-switcher', title: 'Your conversations', body: 'Your open conversations, and how each is doing. Tap to switch between them.' },
            { anchor: 'chat-new-tab', title: 'Start a conversation', body: 'Pick who to talk to. Synapse AI comes first: it can build agents and workflows for you.' },
            { anchor: 'chat-composer', title: 'Ask for what you need', body: 'Type it in your own words. You can attach, paste or drop images when the agent’s model can read them.' },
            { anchor: 'chat-rail', title: 'History, Pinned and Search', body: 'Open an earlier conversation in a tab, pin one to find it again, or search every question and answer.' },
            { anchor: 'help', title: 'Help on every page', body: 'The “?” explains the page you are on. You can take this tour again from there.' },
        ],
    },
    agents: {
        version: 1,
        steps: [
            { anchor: 'agents-new', title: 'Make an agent', body: 'An agent is a model with instructions and tools. Make one yourself, or use the arrow to describe it and let AI write it.' },
            { anchor: 'agents-list', title: 'Your agents', body: 'Pick one to change its instructions, model and tools, and try it in the chat beside its settings.' },
            { anchor: 'rail-chat', title: 'Talk to them', body: 'Chat with any agent, or put it to work as a step in a workflow.' },
        ],
    },
    // The editors, in more detail (F1029; Naveen: "under agents or workflows creations let's have little more detailed
    // guides to make them understand a little"). The canvas's anchors sit on the parts TABS1 v2's editor tabs keep
    // (TopBar, Controls, StepNode).
    'agents.editor': {
        version: 1,
        steps: [
            { anchor: 'setup-model', title: 'The model it thinks with', body: 'Pick the model this agent uses. A stronger model handles harder work and costs more per answer; a small one is fine for simple, repeated jobs.' },
            { anchor: 'setup-instructions', title: 'What it is for', body: 'Say in plain words what the agent does, who it works for, and what it must never do. This is what it reads before every answer.' },
            { anchor: 'setup-tools', title: 'What it may use', body: 'Tick the apps, your own tools, the Vault and the web it may reach. An agent can only do what its tools allow, and a tool that changes something can wait for your approval.' },
            { anchor: 'setup-skills', title: 'What it knows', body: 'Give it skills (ready-made know-how) and knowledge bases, so it answers from your documents and says where the answer came from.' },
            { anchor: 'agent-test', title: 'Try it before you use it', body: 'Ask it something here. It answers as saved, with its tools, so you see how it behaves before it runs in a workflow.' },
            { anchor: 'agent-try', title: 'Try it before you use it', body: 'Open the test chat and ask it something. It answers as saved, with its tools, so you see how it behaves before it runs in a workflow.' },
        ],
    },
    'orchestrations.editor': {
        version: 1,
        steps: [
            { anchor: 'canvas-name', title: 'Your workflow', body: 'A workflow is a set of steps that run in order: agents that think, tools that act, people who approve. Give it a name that says what it does.' },
            { anchor: 'canvas-add-step', title: 'Add a step', body: 'Pick what happens next: an agent, a tool or app, a check, a branch, or a person who answers or approves. Then draw a line from one step to the next.' },
            { anchor: 'canvas-step', title: 'A step', body: 'Click a step to set it up on the right. After a run, each step shows what it did, what it returned and what it cost.' },
            { anchor: 'canvas-change-ai', title: 'Change it with AI', body: 'Describe a change in your own words. Synapse AI shows the change first, and nothing is saved until you say yes.' },
            { anchor: 'canvas-settings', title: 'How it runs', body: 'Its settings: how much one run may spend, how its runs are scored, and the rest of how it behaves.' },
            { anchor: 'canvas-run', title: 'Run it', body: 'Run it now and watch each step light up as it works. If a step needs you, the run waits and asks.' },
        ],
    },
};

/**
 * A section's tabs, one stop each (F1028; Naveen: "when they land on runs page let's show other tabs and its uses and
 * same way let's do the same in other pages explaining each tabs"). Built from the nav, so a tab added there is in its
 * section's tour with no edit here, in the words its own page header uses (the blurb). Each tab of the bar carries
 * `data-tour="tab-<id>"` (`HubTabs`); a tab hidden by a switch is skipped as any absent stop is.
 */
export function tabTourFor(topicId: string): { id: string; tour: Tour } | undefined {
    const hub = HUB_NAV.flatMap(g => g.hubs).find(h => h.tabs.length > 1 && h.tabs.some(t => t.id === topicId));
    if (!hub) return undefined;
    return {
        id: `section-${hub.id}`,
        tour: {
            version: 1,
            steps: hub.tabs.map(t => ({ anchor: `tab-${t.id}`, title: `${hub.label} › ${t.label}`, body: t.blurb })),
        },
    };
}
