/*
 * What an Agent step holds (TEAM1e; Naveen at its plan): an agent, a Team agent — its members working as a team
 * or one at a time — or a workflow wrapped as an agent. Each is picked in the Agent step; the picker groups them,
 * the step on the canvas says which, and its setup shows what goes with it. The words are 20 Q11's: a stored
 * `delegate` reads Team, an `orchestrator` reads Workflow, never the stored type.
 */
import type { ComboboxOption } from '@/components/ui';

import { TEAM_DEFAULTS } from '../team/TeamSetup';

export type AgentKind = 'agent' | 'team' | 'one_at_a_time' | 'workflow';

/** The fields of an agent the step reads: any agent row of either product. */
export type KindAgent = {
    id?: string;
    name?: string;
    type?: string;
    team?: { mode?: string; at_once?: number; task_budget?: number; spend_limit_usd?: number | null } | null;
    delegate_agent_ids?: string[] | null;
};

export function agentKind(agent?: KindAgent | null): AgentKind {
    if (!agent) return 'agent';
    if (agent.type === 'orchestrator') return 'workflow';
    if (agent.type === 'delegate') return agent.team?.mode === 'team' ? 'team' : 'one_at_a_time';
    return 'agent';
}

/** The kind's one word: on the step ("Agent · Team") and in the picker. */
export const KIND_WORD: Record<AgentKind, string> = {
    agent: 'Agent', team: 'Team', one_at_a_time: 'Team', workflow: 'Workflow',
};

const GROUP: Record<AgentKind, string> = {
    agent: 'Agents', team: 'Teams', one_at_a_time: 'Teams', workflow: 'Workflows',
};
const GROUP_ORDER = ['Agents', 'Teams', 'Workflows'];

/** A Team agent's members by name; "any agent" when it names none (its members are then every agent). */
export function memberNames(agent: KindAgent, agents: KindAgent[]): string[] {
    const ids = agent.delegate_agent_ids ?? [];
    return ids.map((id) => agents.find((a) => a.id === id)?.name ?? id);
}

/** What the picker says beside an agent's name: its kind, and for a Team how its members work. */
export function kindHint(agent: KindAgent): string {
    const kind = agentKind(agent);
    if (kind !== 'team' && kind !== 'one_at_a_time') return KIND_WORD[kind];
    const n = (agent.delegate_agent_ids ?? []).length;
    const who = n ? `${n} member${n === 1 ? '' : 's'}` : 'any agent';
    return `Team · ${who}, ${kind === 'team' ? 'as a team' : 'one at a time'}`;
}

/** The Agent step's picker: agents, then Teams, then Workflows, each under its heading. */
export function agentOptions(agents: KindAgent[]): ComboboxOption[] {
    return [...agents]
        .map((a) => ({ a, group: GROUP[agentKind(a)] }))
        .sort((x, y) => GROUP_ORDER.indexOf(x.group) - GROUP_ORDER.indexOf(y.group))
        .map(({ a, group }) => ({ value: String(a.id ?? ''), label: String(a.name ?? a.id ?? ''), hint: kindHint(a), group }));
}

/** A Team agent's settings as it runs them: its own, else the defaults a Team agent starts with. */
export function teamSettings(agent: KindAgent) {
    const team = agent.team ?? {};
    return {
        atOnce: team.at_once ?? TEAM_DEFAULTS.at_once,
        taskBudget: team.task_budget ?? TEAM_DEFAULTS.task_budget,
        spendLimit: team.spend_limit_usd ?? null,
    };
}
