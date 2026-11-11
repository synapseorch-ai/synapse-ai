/*
 * A schedule turned on or paused from its row (F1452), pure so both products and their tests share it.
 *
 * The row's switch moves at once and the product's save follows: while it is out, the row shows what was asked
 * (`asked`), and a refused save puts the row back as it was (`withoutAsk`). An ask is dropped once the product's
 * own list says the same (`settled`): OSS swaps the row for the PATCH's answer, cloud's page reads again, a moment
 * after its save has answered.
 *
 * Pausing is one click. Turning on asks first, with the message each run starts with and when the first one is
 * (F155: a paused schedule is a person's to read before it repeats). An interval's next run is exact — both servers
 * count it from the moment it is turned on — but a cron's is said in its words on its own clock: the servers read a
 * cron with croniter on the schedule's clock (F948), and a second cron reader here could disagree with it on the day
 * the clocks change. The exact time comes in the server's answer.
 */
import { cronWords, type ScheduleLike } from './workflowRows';

/** What each row was asked to be, by schedule id, while its save is out. */
export type EnabledAsks = Readonly<Record<string, boolean>>;

/** The row as it shows: what was asked, else what the product's list says. */
export function shownEnabled(asks: EnabledAsks, id: string, enabled: boolean): boolean {
    return id in asks ? asks[id] : enabled;
}

export function withAsk(asks: EnabledAsks, id: string, on: boolean): EnabledAsks {
    return { ...asks, [id]: on };
}

/** A refused save: the row is what the product's list says again. */
export function withoutAsk(asks: EnabledAsks, id: string): EnabledAsks {
    if (!(id in asks)) return asks;
    const { [id]: _gone, ...rest } = asks;
    void _gone;
    return rest;
}

/** The asks the product's list does not say yet; one whose schedule is gone is dropped too. */
export function settled(asks: EnabledAsks, schedules: readonly { id: string; enabled: boolean }[]): EnabledAsks {
    const byId = new Map(schedules.map((s) => [s.id, s.enabled]));
    const keep = Object.entries(asks).filter(([id, on]) => byId.has(id) && byId.get(id) !== on);
    return keep.length === Object.keys(asks).length ? asks : Object.fromEntries(keep);
}

export type TurnOnNext = { kind: 'at'; iso: string } | { kind: 'words'; words: string };

/** What the turn-on ask says: the message each run starts with (null when it has none) and the first run. */
export function turnOnWords(
    s: ScheduleLike & { prompt?: string | null },
    now: number,
): { message: string | null; next: TurnOnNext } {
    const message = (s.prompt ?? '').trim() || null;
    if (s.schedule_type === 'interval') {
        // As both servers' compute_next_run: at least 1, from now.
        const n = Math.max(s.interval_value ?? 1, 1);
        const unit = s.interval_unit === 'days' ? 86_400_000 : s.interval_unit === 'hours' ? 3_600_000 : 60_000;
        return { message, next: { kind: 'at', iso: new Date(now + n * unit).toISOString() } };
    }
    return { message, next: { kind: 'words', words: cronWords(s.cron_expression, s.timezone) } };
}

/** A refused save's words: the server's own when it gave some. */
export function refusedWords(err: unknown, on: boolean, name: string): string {
    const said = err instanceof Error ? err.message.trim() : typeof err === 'string' ? err.trim() : '';
    return said || `Couldn't ${on ? 'turn on' : 'pause'} ${name}. Try again.`;
}
