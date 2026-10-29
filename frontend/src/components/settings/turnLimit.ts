/**
 * What an agent does at its turn limit (F1219; the engine's `core/turn_limit.py`): after one last turn to answer with
 * what it has, it asks, keeps going within the run's cost cap, or stops. Settings → General sets it for the
 * workspace; an agent's own choice beats it.
 */
import type { SelectOption } from '@/components/ui/select';

export type TurnLimitMode = 'ask' | 'continue' | 'stop';

const MODES: SelectOption[] = [
    { value: 'ask', label: 'Ask me', hint: '“Keep going?” with Keep going or Answer with what you have' },
    { value: 'continue', label: 'Keep going automatically', hint: 'Within the run’s cost cap; with none, up to $1' },
    { value: 'stop', label: 'Stop at the limit', hint: 'Ends as out of turns' },
];

/** Settings → General. The blank default asks where a person is in the turn and stops a run nobody watches. */
export const WORKSPACE_TURN_LIMIT_CHOICES: SelectOption[] = [
    { value: '__default__', label: 'Ask in chats, stop in unattended runs' },
    ...MODES,
];

/** An agent's own: Default follows Settings → General. */
export const AGENT_TURN_LIMIT_CHOICES: SelectOption[] = [
    { value: '__default__', label: 'Default (Settings → General)' },
    ...MODES,
];
