/*
 * The guided tours (GUIDE1, D318, F920): a few stops on a main page, each lighting up one part and saying what it
 * is for. Naveen: "a highlighter will highlight a section and explain what we can do with this ... and they just
 * click next". Keyed by the page's help topic id (`lib/helpHost.tsx`). A stop whose part isn't on the page right
 * now (a new workspace's empty state, a phone's hidden rail) is skipped, so one list serves every state.
 *
 * Every anchor must exist in the source (`test_every_tour_step_has_its_anchor.py`): a screen that drops one fails a
 * test instead of quietly losing its stop. Raise a tour's `version` when its words change enough to show it again.
 */
import type { Tour } from '@/components/help';

export const TOURS: Record<string, Tour> = {
    dashboard: {
        version: 1,
        steps: [
            { anchor: 'home-ask', title: 'Ask Synapse AI', body: 'Ask about your runs in plain words, or describe something to build. It answers from what your workspace has done.' },
            { anchor: 'home-start', title: 'Your first steps', body: 'A short path from connecting a model to a workflow that runs on its own. Each step says what to do next.' },
            { anchor: 'home-needs-you', title: 'What needs you', body: 'When a workflow stops to ask a question or wait for your approval, it shows here. Answer it without leaving Home.' },
            { anchor: 'home-more', title: 'More you can do', body: 'Parts of Synapse this workspace hasn’t tried yet, such as scoring runs or answering from your documents.' },
            { anchor: 'rail-runs', title: 'Runs, scores and costs', body: 'Every run, its log, its score (Evals) and what it cost (Usage) live under Runs.' },
            { anchor: 'rail-ai_tools', title: 'Use it from your AI tools', body: 'Connect Claude, Codex, Cursor or VS Code, and run your workflows from there.' },
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
    agents: {
        version: 1,
        steps: [
            { anchor: 'agents-new', title: 'Make an agent', body: 'An agent is a model with instructions and tools. Make one yourself, or use the arrow to describe it and let AI write it.' },
            { anchor: 'agents-list', title: 'Your agents', body: 'Pick one to change its instructions, model and tools, and try it in the chat beside its settings.' },
            { anchor: 'rail-chat', title: 'Talk to them', body: 'Chat with any agent, or put it to work as a step in a workflow.' },
        ],
    },
};
