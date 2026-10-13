/*
 * Why a run stopped, in one plain sentence, and what to do about it (M2b).
 *
 * UX1 measured a failed run's page leading with a raw Python traceback three
 * times, in red, before any plain reason. The engine keeps a step's own words
 * (the history entry's `error`, F195) and the run's (`failure_reason`, F257);
 * this reads them and says what happened as a person would. The words it read
 * are never dropped: the run page keeps them, unedited, under Technical details.
 *
 * The kinds, in the order they are tried, come from a read of every writer of a
 * step's or run's error in the OSS engine (M2b's plan, "Failure sentences"):
 * the step executors, the engine's limits and routes, the model providers'
 * adapters, cancel, the restart sweep. Two reasons come from a host instead of
 * the engine: cloud's model wallet and its credit gate (F419, F444), which stops
 * a run through the engine's `run_gate` seam. Anything unmatched falls to the last
 * two rows: "<step> stopped this run with an error it didn't expect", with the
 * reason's headline (`lib/runErrors.ts`'s `reasonHeadline`, F195's rule, which
 * both products keep word for word), or "stopped without recording why".
 */
import { reasonHeadline } from '@/lib/runErrors';

import { duration, type NestedRun, type StepExecution } from './model';

/** What a failure card can offer; the page shows the ones its product can link. */
export type FixKind =
    | 'editor' | 'replay' | 'models' | 'agents' | 'knowledge' | 'billing' | 'credits'
    | 'nested' | 'parent' | 'log';

export type Explained = {
    /** A short id for the kind, for tests and the card's `data-kind`. */
    kind: string;
    /** The one plain sentence the card leads with. */
    sentence: string;
    /** A second line, when the reason holds something worth saying (the line of code, the provider's words). */
    detail?: string;
    /** What to do, in words, when a button alone would not say it. */
    advice?: string;
    /** A line of the step's own code, shown as code. */
    code?: string;
    fixes: FixKind[];
    /** The nested run to open, for a nested workflow's failure. */
    nestedRunId?: string;
};

const stripClass = (s: string) => s.replace(/^(RuntimeError|LLMError|EmptyAnswer|EmbeddingFailed): /, '');

const PROVIDERS: [RegExp, string][] = [
    // The hosted providers (PROV1) are read only in the engine's own words for them ("Groq LLM Error: …",
    // "Error: Groq returned …", "Empty Groq response"): an Ollama error can name a model called mistral or qwen.
    ...([['Moonshot (Kimi)', 'Moonshot'], ['Mistral', 'Mistral'], ['NVIDIA', 'NVIDIA'], ['OpenRouter', 'OpenRouter'],
        ['Qwen', 'Qwen'], ['Z.ai', 'Z.ai'], ['Groq', 'Groq'], ['Cerebras', 'Cerebras'], ['Together AI', 'Together AI'],
        ['Fireworks', 'Fireworks'], ['MiniMax', 'MiniMax']] as const).map(([label, name]): [RegExp, string] => {
        const l = label.replace(/[.()]/g, '\\$&');
        return [new RegExp(`${l} LLM Error|Error: ${l} (?:returned|refused)|Empty ${l} response`), name];
    }),
    [/\bOpenAI\b/, 'OpenAI'], [/\bClaude\b|Anthropic/, 'Anthropic'], [/\bGrok\b/, 'xAI'],
    [/\bDeepSeek\b/, 'DeepSeek'], [/\bBedrock\b/, 'Amazon Bedrock'], [/\bGemini\b/, 'Google Gemini'],
    [/Ollama/, 'Ollama'], [/Model gateway/, 'The model gateway'],
];

function providerOf(reason: string): string {
    return PROVIDERS.find(([re]) => re.test(reason))?.[1] ?? 'The model provider';
}

/** A provider's own last words: after "Last error:", else the reason's headline. */
function lastError(reason: string): string {
    const m = /Last error: ([\s\S]+)$/.exec(reason);
    return (m ? m[1] : reasonHeadline(stripClass(reason))).trim().slice(0, 240);
}

const MODEL_FAILURE = /^(LLMError|EmptyAnswer|EmbeddingFailed): |LLM Error: |^(?:RuntimeError: )?(LLM|Tool) step '.+' (LLM call )?failed: |Cloud API Error|CLI Provider Error|CLI provider '.+' failed|HuggingFace Provider Error|Bedrock refused|Model gateway refused|model wallet has|Unknown cloud model selected|Local Agent Error: /;

/**
 * Cloud's credit gate (`worker/credit_gate.py`, F419), in its own words. Unanchored: the gate stops a run between
 * steps (the run's reason), in a Loop body ("Stopped: …" on the Loop; "RuntimeError: Stopped: …" before F407), in a Parallel branch ("Branch 2
 * failed: …") and in a workflow a step runs ("Sub-orchestration 'X' failed: …"). The numbers are optional: a reason
 * wrapped that deep can lose its tail to the engine's 300-character cap.
 */
const CREDIT_STOP = /Stopped: this workspace is out of execution credits\.(?: This run needed about ([\d,]+) credits and ([\d,]+) were left for it\.)?/;

/** Whether the credit gate stopped this run: the run list says so in place of "Failed" (F444). */
export function isCreditStop(reason: string | null | undefined): boolean {
    return !!reason && CREDIT_STOP.test(reason);
}

/** The parts of a run a sentence can name. */
export type FailureInput = {
    status: string;
    /** The failed step's own error, else the run's. */
    reason: string | null | undefined;
    /** The step it stopped at, when one did. */
    stepName?: string | null;
    stepId?: string | null;
    runId: string;
    /** The newest history entry, for a cancel that closed a question. */
    newest?: StepExecution | null;
    children?: NestedRun[];
};

/** The run that started this one, from its id (`{parent}__{step}_d{n}_{hex}`). */
export function parentRunId(runId: string): string | null {
    const i = runId.lastIndexOf('__');
    return i > 0 ? runId.slice(0, i) : null;
}

/** The plain sentence and its fix, for a failed or cancelled run. */
export function explainFailure(input: FailureInput): Explained {
    const raw = (input.reason ?? '').trim();
    const step = input.stepName || 'A step';
    const its = input.stepName ? `${input.stepName}’s` : 'A step’s';

    // ── The run's own reasons: who stopped it, and the limits around it ──
    let m: RegExpExecArray | null;
    if ((m = /^Stopped: the run that called it (failed|was cancelled|had already ended)(?: — ([\s\S]+))?/.exec(raw))) {
        return {
            kind: 'parent-stopped',
            sentence: `The run that called this one ${m[1] === 'failed' ? 'failed' : m[1] === 'was cancelled' ? 'was cancelled' : 'had already ended'}, so this one stopped too.`,
            detail: m[2] ? reasonHeadline(m[2]) : undefined,
            fixes: ['parent'],
        };
    }
    if (input.status === 'cancelled') {
        if (/conversation moved on before its question was answered/.test(raw)) {
            return { kind: 'cancelled-chat', sentence: 'The chat moved on before this run’s question was answered.', fixes: ['replay'] };
        }
        const waited = input.newest && input.newest.status === 'cancelled';
        return {
            kind: 'cancelled',
            sentence: waited && input.newest?.step_name
                ? `Someone cancelled this run while it waited for ${input.newest.step_name}’s answer.`
                : 'Someone cancelled this run.',
            detail: raw ? reasonHeadline(raw) : undefined,
            fixes: ['replay'],
        };
    }
    // Before the containers: the workspace ran out, not the branch, the loop or the nested workflow (F444).
    if ((m = CREDIT_STOP.exec(raw))) {
        return {
            kind: 'credits',
            sentence: 'This workspace is out of execution credits.',
            detail: m[1] ? `This run needed about ${m[1]} credits and ${m[2]} were left for it.` : undefined,
            fixes: ['credits', 'replay'],
        };
    }
    if (/Server restarted while the run was in progress/.test(raw)) {
        return { kind: 'restart', sentence: 'The server restarted while this run was going.', advice: 'Replay resumes it at the step it had reached.', fixes: ['replay'] };
    }
    if ((m = /^Global timeout \((\d+) min\) exceeded after ([\d.]+) min of work/.exec(raw))) {
        return {
            kind: 'run-timeout',
            sentence: `This run used up its ${m[1]}-minute timeout (${m[2]} min of work; time waiting for a person doesn’t count).`,
            advice: 'Raise Timeout (minutes) in the workflow’s Guardrails, then Replay.',
            fixes: ['editor', 'replay'],
        };
    }
    if ((m = /^Stopped at (.+?), after ([\d.]+) min of work/.exec(raw))) {
        return {
            kind: 'run-ceiling',
            sentence: `This run reached ${m[1]} after ${m[2]} min of work.`,
            advice: 'Time waiting for a person doesn’t count toward it.',
            fixes: ['replay'],
        };
    }
    if ((m = /Global turn limit \((\d+)\) exceeded/.exec(raw))) {
        return {
            kind: 'turn-limit',
            sentence: `This run used all ${m[1]} of its turns.`,
            advice: 'Raise Max Total Turns in the workflow’s Guardrails, then Replay.',
            fixes: ['editor', 'replay'],
        };
    }
    if ((m = /Step '(.+?)' wrote text that will not become the declared type of '([^']+)' \(([^)]+)\)/.exec(raw))) {
        return {
            kind: 'strict-types',
            sentence: `${m[1]} wrote text that isn’t a valid ${m[3]} for ${m[2]}, and this workflow stops on a type mismatch.`,
            advice: 'Change what the step writes, or turn off “Stop on a state type mismatch” in Guardrails.',
            fixes: ['editor'],
        };
    }
    if (/Step '.+?' (in branch \d+ )?not found|Loop body step '.+?' not found|No executor for/.test(raw) && !/Agent '/.test(raw)) {
        return { kind: 'missing-step', sentence: 'The workflow points to a step that no longer exists.', detail: reasonHeadline(stripClass(raw)), fixes: ['editor'] };
    }

    // ── Routes ──
    if ((m = /'(.+?)' gave up after (\d+) attempts — ([\s\S]+)$/.exec(raw))) {
        return {
            kind: 'route-gave-up',
            sentence: `${m[1]} went round ${m[2]} times without a way out.`,
            advice: capital(m[3].trim()),
            fixes: ['editor', 'replay'],
        };
    }
    if ((m = /'(.+?)' reached its Max iterations \((\d+)\), and '(.+?)', where it goes then, has used its own \((\d+)\)/.exec(raw))) {
        return {
            kind: 'route-gave-up',
            sentence: `${m[1]} reached its Max iterations (${m[2]}), and so did ${m[3]}, where it goes next (${m[4]}).`,
            advice: 'Raise one of them, then Replay.',
            fixes: ['editor', 'replay'],
        };
    }
    if ((m = /^No case of '(.+?)' matched ([\s\S]+?) and it has no default path/.exec(raw))) {
        return {
            kind: 'switch-no-case',
            sentence: `${m[1]} matched none of its cases, and it has no default path.`,
            detail: /could not be evaluated/.test(m[2]) ? `Its value ${m[2]}.` : `The value was ${m[2]}.`,
            advice: 'Add a case or a default, then Replay.',
            fixes: ['editor', 'replay'],
        };
    }

    // ── Containers: a branch, a loop, a nested workflow ──
    if ((m = /^(?:RuntimeError: )?(?:(\d+) branches failed; first: )?Branch (\d+) failed: ([\s\S]+)$/.exec(raw))) {
        const inner = explainFailure({ ...input, status: 'failed', reason: m[3], stepName: null });
        const more = m[1] ? ` (and ${Number(m[1]) - 1} more)` : '';
        return { ...inner, kind: `branch:${inner.kind}`, sentence: `Branch ${m[2]} of ${step} failed${more}.`, detail: inner.sentence, advice: inner.advice };
    }
    if ((m = /Branches (.+?) of '(.+?)' (both|all) write '([^']+)'/.exec(raw))) {
        return {
            kind: 'parallel-clash',
            sentence: `Branches ${m[1]} of ${m[2]} ${m[3]} write ${m[4]}, so only one value would survive.`,
            advice: 'Give each branch its own output key.',
            fixes: ['editor'],
        };
    }
    if ((m = /(\d+) items exceed the (\d+)-iteration cap of '(.+?)'/.exec(raw)) || (m = /A count of (\d+) exceeds the (\d+)-iteration cap of '(.+?)'/.exec(raw))) {
        const items = /items exceed/.test(raw);
        return {
            kind: 'loop-cap',
            sentence: items ? `${m[3]} had ${m[1]} items, more than its cap of ${m[2]}.` : `${m[3]} was asked to run ${m[1]} times, more than its cap of ${m[2]}.`,
            advice: 'Raise this loop’s Max iterations, then Replay.',
            fixes: ['editor', 'replay'],
        };
    }
    if ((m = /Loop nesting exceeded (\d+) levels at '(.+?)'/.exec(raw))) {
        return { kind: 'loop-nesting', sentence: `Loops inside ${m[2]} go more than ${m[1]} levels deep, so it would never end.`, fixes: ['editor'] };
    }
    if ((m = /Sub-orchestration '(.+?)'((?: › '.+?')*) (failed|was cancelled): ([\s\S]*)$/.exec(raw))) {
        const name = m[1] + m[2].replace(/ › '(.+?)'/g, ' › $1');
        const child = nestedChild(input);
        const inner = explainFailure({ status: m[3] === 'failed' ? 'failed' : 'cancelled', reason: m[4], runId: child ?? input.runId, stepName: null });
        return {
            kind: 'nested-failed',
            sentence: `The workflow ${name}, which ${step} runs, ${m[3] === 'failed' ? 'failed' : 'was cancelled'}.`,
            detail: inner.sentence,
            fixes: child ? ['nested', 'replay'] : ['replay'],
            nestedRunId: child ?? undefined,
        };
    }
    if ((m = /Sub-orchestration '(.+?)' (could not be resumed|ended without a final response|it ended without finishing)/.exec(raw)) || /Nested orchestration failed to produce a result after human input/.test(raw)) {
        const name = m?.[1] ?? 'it runs';
        const child = nestedChild(input);
        return {
            kind: 'nested-failed',
            sentence: !m
                ? `The workflow ${step} runs finished without an answer after its question was answered.`
                : m[2] === 'could not be resumed'
                    ? `The workflow ${name}, which ${step} runs, couldn’t carry on after its question was answered.`
                    : `The workflow ${name}, which ${step} runs, finished without an answer.`,
            detail: reasonHeadline(stripClass(raw)),
            fixes: child ? ['nested', 'replay'] : ['replay'],
            nestedRunId: child ?? undefined,
        };
    }
    if ((m = /depth limit exceeded \(max (\d+)\)/.exec(raw))) {
        return { kind: 'nested-depth', sentence: `Workflows call each other more than ${m[1]} levels deep here.`, advice: 'An agent that runs a workflow is probably calling itself.', fixes: ['editor'] };
    }
    if (/has no orchestration_id/.test(raw)) {
        return { kind: 'nested-unset', sentence: `${its} agent runs a workflow, but none is set.`, fixes: ['agents'] };
    }
    if ((m = /Sub-orchestration '(.+?)' \(from agent '(.+?)'\) not found/.exec(raw))) {
        return { kind: 'nested-missing', sentence: `The workflow ${its} agent runs no longer exists.`, fixes: ['agents'] };
    }

    // ── Knowledge ──
    if ((m = /Knowledge is switched off, so (Retrieve|Ingest) step '(.+?)' cannot run/.exec(raw))) {
        return { kind: 'knowledge-off', sentence: `Knowledge is switched off, so ${m[2]} can’t run.`, fixes: ['knowledge'] };
    }
    if ((m = /(Retrieve|Ingest) step '(.+?)' is not set up: ([\s\S]+)$/.exec(raw))) {
        return { kind: 'knowledge-setup', sentence: `${m[2]} isn’t set up.`, detail: reasonHeadline(m[3]), fixes: ['editor'] };
    }
    if ((m = /Retrieve step '(.+?)': the query is empty/.exec(raw))) {
        return { kind: 'knowledge-empty-query', sentence: `${m[1]}’s search came out empty once the run’s values were filled in.`, advice: 'Check the step’s query and the state keys it reads.', fixes: ['editor'] };
    }
    if ((m = /(Retrieve|Ingest) step '(.+?)': there is no knowledge base/.exec(raw))) {
        return { kind: 'knowledge-missing', sentence: `${m[2]}’s knowledge base no longer exists.`, fixes: ['editor', 'knowledge'] };
    }
    if ((m = /(Retrieve|Ingest) step '(.+?)': knowledge base '(.+?)' is disabled/.exec(raw))) {
        return { kind: 'knowledge-disabled', sentence: `${m[2]}’s knowledge base, ${m[3]}, is switched off.`, fixes: ['knowledge'] };
    }
    if ((m = /(Retrieve|Ingest) step '(.+?)'(?::| is pinned) ([\s\S]+)$/.exec(raw))) {
        return {
            kind: 'knowledge-other',
            sentence: `${m[2]} couldn’t ${m[1] === 'Retrieve' ? 'search its knowledge base' : 'add its records'}.`,
            detail: reasonHeadline(m[3]),
            fixes: ['editor', 'replay'],
        };
    }

    // ── Python code (Transform) ──
    if ((m = /Transform error: Transform timed out after (\d+)s/.exec(raw))) {
        return { kind: 'code-timeout', sentence: `${its} Python code ran longer than its ${m[1]}-second limit.`, fixes: ['editor', 'replay'] };
    }
    if (/Docker is not available/.test(raw)) {
        return { kind: 'code-no-docker', sentence: `${step} couldn’t run its Python code: Docker isn’t running on this machine.`, advice: 'Start Docker, then Replay.', fixes: ['replay'] };
    }
    if ((m = /Transform( \(host\))? failed on line (\d+)(?: \(`([^`]*)`\))?: ([\s\S]+)$/.exec(raw))) {
        // The engine names your line, its source and what it raised (F407).
        return {
            kind: 'code-error',
            sentence: `${its} Python code raised an error on line ${m[2]}.`,
            code: m[3] || undefined,
            detail: `It raised ${m[4].trim()}.`,
            fixes: ['editor', 'replay'],
        };
    }
    if (/Transform error: Transform( \(host\))? failed/.test(raw)) {
        // A run from before F407: the sandbox's traceback, whose line is the wrapper's.
        const line = [...raw.matchAll(/script\.py", line (\d+)/g)].pop();
        const code = /script\.py", line \d+[^\n]*\n\s*([^\n]+)/.exec(raw.slice(line?.index ?? 0));
        const raised = /\n\s*([A-Z]\w*(?:Error|Exception)\b[^\n]*)\s*$/.exec(raw);
        return {
            kind: 'code-error',
            sentence: `${its} Python code raised an error${line ? ` on line ${line[1]}` : ''}.`,
            code: code ? code[1].trim() : undefined,
            detail: raised ? `It raised ${raised[1].trim()}.` : undefined,
            fixes: ['editor', 'replay'],
        };
    }

    // ── Tools (a tool's own failure, before a model's: a Tool step names both) ──
    if ((m = /did not run '(.+?)': a person refused it(?:: ([\s\S]+))?/.exec(raw))) {
        return { kind: 'tool-refused', sentence: `Someone refused ${its} call to ${m[1]}.`, detail: m[2] ? m[2].trim() : undefined, fixes: ['replay'] };
    }
    if ((m = /did not run '(.+?)': the tool policy blocks it because ([\s\S]+)$/.exec(raw))) {
        return { kind: 'tool-policy', sentence: `The tool policy blocks ${m[1]}.`, detail: capital(m[2].trim()), fixes: ['replay'] };
    }
    if (/has no tool configured/.test(raw)) {
        return { kind: 'tool-unset', sentence: `${step} has no tool set.`, fixes: ['editor'] };
    }
    if ((m = /Tool '(.+?)' not found in (available tools|tool router)/.exec(raw))) {
        return { kind: 'tool-missing', sentence: `${m[1]} isn’t available here any more.`, advice: 'Connect it again, or pick another tool for the step.', fixes: ['editor'] };
    }
    if ((m = /uses '(.+?)', which visitors on this (\w+) may not use/.exec(raw))) {
        return { kind: 'tool-visitor', sentence: `${step} uses ${m[1]}, which visitors on this ${m[2]} may not use.`, fixes: ['editor'] };
    }
    if ((m = /Tool step '(.+?)' failed after (\d+) attempt\(s\): ([\s\S]+)$/.exec(raw))) {
        const inner = m[3];
        let t: RegExpExecArray | null;
        if ((t = /Tool '(.+?)' returned an error: ([\s\S]+)$/.exec(inner))) {
            return { kind: 'tool-error', sentence: `${m[1]} couldn’t use ${t[1]}.`, detail: reasonHeadline(t[2]), fixes: ['replay', 'editor'] };
        }
        if ((t = /HTTP (\d+) from '(.+?)'/.exec(inner))) {
            return { kind: 'tool-http', sentence: `${t[2]} answered ${m[1]} with HTTP ${t[1]}.`, detail: reasonHeadline(inner), fixes: ['replay', 'editor'] };
        }
        if ((t = /Python tool exited with code (\d+)/.exec(inner))) {
            return { kind: 'tool-python', sentence: `${m[1]}’s Python tool stopped with an error (exit ${t[1]}).`, detail: reasonHeadline(inner), fixes: ['editor', 'replay'] };
        }
        if (/did not return a valid tool call JSON/.test(inner)) {
            return { kind: 'tool-bad-call', sentence: `The model didn’t write a valid call for ${m[1]}’s tool, ${m[2]} times.`, fixes: ['replay', 'editor'] };
        }
        if (!MODEL_FAILURE.test(inner)) {
            return { kind: 'tool-error', sentence: `${m[1]}’s tool call failed.`, detail: reasonHeadline(inner), fixes: ['replay', 'editor'] };
        }
    }

    // ── Models ──
    if (MODEL_FAILURE.test(raw)) {
        const who = providerOf(raw);
        if (/model wallet has .*(left|available)/i.test(raw)) {
            return { kind: 'wallet', sentence: 'This run stopped because the model wallet ran out.', fixes: ['billing', 'replay'] };
        }
        if (/\b402\b|insufficient_quota|\bcredit\b|prepay/i.test(raw)) {
            return { kind: 'model-credit', sentence: `${who} refused the call: the account is out of credit.`, detail: lastError(raw), fixes: ['models', 'replay'] };
        }
        if (/\b401\b|unauthori[sz]ed|invalid[ _-]?api[ _-]?key|authentication method|Missing key inputs|IAM credentials/i.test(raw)) {
            return { kind: 'model-key', sentence: `${who} rejected the API key ${step === 'A step' ? 'the step' : step} used.`, detail: lastError(raw), fixes: ['models', 'replay'] };
        }
        if (/(HTTP|API error) 429\b|429 RESOURCE_EXHAUSTED|rate.?limit/i.test(raw)) {
            const tries = /failed after (\d+) attempt/.exec(raw)?.[1];
            return {
                kind: 'model-rate-limit',
                sentence: `${who} kept answering “too many requests”${tries ? `, ${tries} times in a row` : ''}.`,
                advice: 'Wait a minute, then Replay.',
                fixes: ['replay'],
            };
        }
        if ((m = /Unknown cloud model selected \((.+?)\)/.exec(raw))) {
            return { kind: 'model-unknown', sentence: `${step} uses a model this workspace can’t reach: ${m[1]}.`, advice: 'Pick another model for the step.', fixes: ['editor', 'models'] };
        }
        if ((m = /Ollama is not reachable at (\S+?)\.?(?:\s|$)/.exec(raw))) {
            return { kind: 'model-ollama-down', sentence: `Ollama isn’t reachable at ${m[1]}.`, advice: 'Start Ollama, then Replay.', fixes: ['replay', 'models'] };
        }
        if ((m = /Ollama at (\S+) answered \d+: model '(.+?)' not found/.exec(raw))) {
            return { kind: 'model-unknown', sentence: `Ollama at ${m[1]} doesn’t have the model ${m[2]}.`, advice: 'Pull the model, or pick another one for the step.', fixes: ['editor', 'models'] };
        }
        if (/prompt is too long|maximum context length|context_length_exceeded|exceeds the maximum number of tokens|too many tokens/i.test(raw)) {
            return { kind: 'model-context', sentence: `${step} sent the model more text than it can read at once.`, advice: 'Send less into the step, or pick a model with a longer context.', fixes: ['editor'] };
        }
        if (/^EmptyAnswer: |withheld this reply|returned only reasoning|refused this request|returned no usable content|blocked by Gemini safety|No response candidates|Empty .+ response/.test(raw)) {
            if ((m = /withheld this reply \((.+?)\)/.exec(raw)) || /safety filters/.test(raw)) {
                return { kind: 'model-withheld', sentence: `The model withheld its reply to ${step}${m ? ` (${m[1]})` : ' (safety)'}.`, advice: 'Rephrase the step’s prompt, or pick another model.', fixes: ['editor', 'replay'] };
            }
            if (/only reasoning/.test(raw)) {
                // The cap is the agent's (Max response length) or the workspace's; a step has none (F459).
                return { kind: 'model-thinking', sentence: `The model spent its whole reply to ${step} thinking.`, advice: 'Raise Max response length on its agent, or in Settings → Models, then Replay.', fixes: ['agents', 'models', 'replay'] };
            }
            if ((m = /refused this request: ([\s\S]+)$/.exec(raw))) {
                return { kind: 'model-refused', sentence: `The model refused ${its} request.`, detail: reasonHeadline(m[1]), fixes: ['editor', 'replay'] };
            }
            return { kind: 'model-empty', sentence: `The model gave ${step} no answer.`, advice: 'Replay; if it happens again, rephrase the step’s prompt or pick another model.', fixes: ['replay', 'editor'] };
        }
        return { kind: 'model-error', sentence: `${its} call to the model failed.`, detail: lastError(raw), fixes: ['replay'] };
    }

    // ── Agents ──
    // A turn the agent loop ended without an answer (F413): the step fails rather than pass the loop's words on.
    if ((m = /Agent step '(.+?)' stopped without an answer: it called (.+?) with the same arguments (\d+) times in a row/.exec(raw))) {
        return {
            kind: 'agent-repeating',
            sentence: `${m[1]}’s agent called ${m[2]} ${m[3]} times in a row with the same arguments and got the same result, so it stopped without an answer.`,
            advice: 'Say in the step’s prompt what to do with that result, or give the agent other tools, then Replay.',
            fixes: ['editor', 'replay'],
        };
    }
    if ((m = /Agent step '(.+?)' ran out of turns \((\d+)\) before answering/.exec(raw))) {
        return {
            kind: 'agent-turns',
            sentence: `${m[1]}’s agent used all ${m[2]} of its turns without answering.`,
            advice: 'Raise Max turns in the step’s Guardrails, or ask for less in its prompt, then Replay.',
            fixes: ['editor', 'replay'],
        };
    }
    if (/ended without producing a final response/.test(raw)) {
        return { kind: 'agent-no-answer', sentence: `${its} agent finished without an answer.`, fixes: ['replay'] };
    }
    if (/No agent to answer yet/.test(raw)) {
        return { kind: 'agent-none', sentence: 'There’s no agent to answer yet.', advice: 'Create one in Agents, or ask the AI builder to make one.', fixes: ['agents'] };
    }
    if (/Agent '.+?' not found/.test(raw)) {
        return { kind: 'agent-missing', sentence: `${its} agent no longer exists.`, advice: 'Pick another agent for the step.', fixes: ['editor'] };
    }

    // ── A step that timed out (a step's own entry, or a body step's container) ──
    if ((m = /timed out after ([\d.]+)s(?: \(([\s\S]+)\))?/i.exec(raw))) {
        const limit = m[2] ? limitWords(m[2]) : null;
        // A host's run limit (a plan's) is not the workflow's to raise (F420).
        const host = !!m[2] && /^what was left of the .+ run limit\b/.test(m[2]);
        return {
            kind: 'step-timeout',
            sentence: `${step} ran out of time after ${duration(Number(m[1]))}.`,
            detail: limit ? `The limit was ${limit}.` : undefined,
            advice: host
                ? 'Time waiting for a person doesn’t count toward it.'
                : 'Raise Timeout (minutes) in the workflow’s Guardrails, then Replay.',
            fixes: host ? ['replay'] : ['editor', 'replay'],
        };
    }

    if (raw) {
        return {
            kind: 'unexpected',
            sentence: input.stepName ? `${step} stopped this run with an error it didn’t expect.` : 'This run stopped with an error it didn’t expect.',
            detail: reasonHeadline(raw),
            fixes: ['replay'],
        };
    }
    return { kind: 'unrecorded', sentence: 'This run stopped without recording why.', advice: 'Its activity log below may say more.', fixes: ['log', 'replay'] };
}

/** The timeout a step had, in words: "what was left of this run's own timeout" → "what was left of the run's timeout". */
function limitWords(why: string): string {
    const own = /the workflow '(.+?)''?s own (\d+)-minute timeout/.exec(why);
    if (own) return `the workflow’s own ${own[2]}-minute timeout`;
    if (/what was left of this run's own timeout/.test(why)) return 'what was left of the run’s timeout';
    return why.replace(/^what was left of /, 'what was left of ');
}

function capital(s: string): string {
    return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** The nested run the failed step started, when the run lists one. */
function nestedChild(input: FailureInput): string | null {
    const kids = input.children ?? [];
    const mine = input.stepId
        ? kids.filter((c) => c.run_id.startsWith(`${input.runId}__${input.stepId}_d`))
        : kids;
    const failed = mine.filter((c) => c.status === 'failed' || c.status === 'cancelled');
    return (failed[failed.length - 1] ?? mine[mine.length - 1])?.run_id ?? null;
}
