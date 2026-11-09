'use client';

/**
 * The first-run model step (ONB2, F728, D303): cloud's questionnaire model step
 * (ONB1, D295), for an open-source install whose model cannot answer — the
 * installer's question skipped, an install from source or Docker, or a local
 * model that is not there.
 *
 * Two screens. "Which AI should Synapse use?" lists what is on this computer and
 * needs no key — a model CLI (signed out: its own hint), Ollama's models — and
 * the providers a key connects; a picked provider opens Settings → Models' own
 * card below, whose field is the only place a key is typed. With nothing at all,
 * it offers two ways to start, each with its trade-off. "Pick your two models"
 * then sets the everyday model (`model`) and Synapse AI's (`builder_model`, empty
 * when the same).
 *
 * A click outside never closes it (cloud's F726: one stray click lost a key half
 * pasted); its ✕, Escape and "Don't show again" do.
 */
import * as RDialog from '@radix-ui/react-dialog';
import { Check, ExternalLink, RefreshCw, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import { ProviderCard } from '@/components/providers/ProviderCard';
import { pickSecrets, type SecretState } from '@/components/settings/SavedSecret';
import { AppMark, Button, Combobox, ErrorNote, Field, buttonStyles } from '@/components/ui';
import { cn, focusRing } from '@/lib/cn';
import { providerLabel } from '@/lib/models';
import {
    FIRST_PROVIDERS, FREE_KEY_PROVIDER, KEY_NAMES, ONBOARDING_PROVIDERS, foundHere, keyNote, nothingYet, pickable,
    startingModel, type FoundHere,
} from '@/lib/modelSetup';
import { KEY_LABELS, OLLAMA_IMG, PROVIDER_META, cardMeta, cardStatus, secretOf, type ProviderInfo } from '@/lib/providerCards';
import { clearSecret, postSecret } from '@/lib/providerSecrets';

type Step = 'providers' | 'models';

/** Synapse AI's "Same as everyday model": `builder_model` stored empty. */
const SAME = '';

/** Each option wears its provider's mark and is found by its provider's name (PICK1, F1482). */
const SEARCH = 'Search models or providers…';

const EMPTY: ProviderInfo = { available: false, models: [] };

const tile = (pressed: boolean) => cn(
    'flex touch-manipulation items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors',
    focusRing,
    pressed ? 'border-accent/40 bg-accent/10' : 'border-border bg-surface hover:border-border-strong',
);

export function ModelSetup({ open, problem, onClose, onSaved }: {
    open: boolean;
    /** Why the workspace's model cannot answer, from `/api/onboarding/models`. */
    problem: string | null;
    /** Its ✕, Escape or "Don't show again". */
    onClose: () => void;
    /** The two models were saved. */
    onSaved: () => void;
}) {
    const [step, setStep] = useState<Step>('providers');
    const [providers, setProviders] = useState<Record<string, ProviderInfo> | null>(null);
    const [secrets, setSecrets] = useState<Record<string, SecretState>>({});
    const [current, setCurrent] = useState<{ model?: string; builder_model?: string }>({});
    const [picked, setPicked] = useState<string[]>([]);
    const [collapsed, setCollapsed] = useState<string[]>([]);
    const [more, setMore] = useState(false);
    const [path, setPath] = useState<'key' | 'ollama' | null>(null);
    const [everyday, setEveryday] = useState<string | null>(null);
    const [assistant, setAssistant] = useState(SAME);
    const [checking, setChecking] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');

    const read = useCallback(async (refresh = false) => {
        const [models, settings] = await Promise.all([
            fetch(`/api/models${refresh ? '?refresh=1' : ''}`).then(r => r.json()),
            fetch('/api/settings').then(r => r.json()),
        ]);
        setProviders(models.providers ?? {});
        setSecrets(pickSecrets(settings, KEY_NAMES));
        setCurrent({ model: settings.model, builder_model: settings.builder_model });
        return { secrets: pickSecrets(settings, KEY_NAMES) };
    }, []);

    // Each opening starts at the first screen, with what is saved already picked.
    useEffect(() => {
        if (!open) return;
        let cancelled = false;
        setStep('providers');
        setEveryday(null);
        setError('');
        read()
            .then(({ secrets: held }) => {
                if (cancelled) return;
                setPicked(ONBOARDING_PROVIDERS.filter(k => held[secretOf(k)!]?.set));
            })
            .catch(() => !cancelled && setError('Could not read the models this install can reach.'));
        return () => { cancelled = true; };
    }, [open, read]);

    async function checkAgain() {
        setChecking(true);
        try { await read(true); } catch { setError('Could not read the models this install can reach.'); }
        finally { setChecking(false); }
    }

    /** After a card saves or removes a key: its models, and "Key saved", follow. */
    const afterKey = async (save: () => Promise<Record<string, unknown>>) => {
        const settings = await save();
        setSecrets(pickSecrets(settings, KEY_NAMES));
        await read();
    };

    const toggle = (key: string) =>
        setPicked(p => (p.includes(key) ? p.filter(k => k !== key) : [...p, key]));

    const options = providers ? pickable(providers) : [];

    function toModels() {
        setEveryday(startingModel(current.model, options));
        setAssistant(current.builder_model && options.some(o => o.value === current.builder_model) ? current.builder_model : SAME);
        setStep('models');
    }

    async function save() {
        if (!everyday) return;
        setBusy(true);
        setError('');
        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: everyday, builder_model: assistant, first_run_models_closed: true }),
            });
            if (!res.ok) throw new Error();
            onSaved();
        } catch {
            setError('The models could not be saved.');
        } finally {
            setBusy(false);
        }
    }

    const found = providers ? foundHere(providers) : [];
    const empty = providers ? nothingYet(providers, secrets) : false;
    const shown = more ? ONBOARDING_PROVIDERS : [...FIRST_PROVIDERS, ...picked.filter(k => !FIRST_PROVIDERS.includes(k))];
    const index = step === 'providers' ? 0 : 1;

    return (
        <RDialog.Root open={open} onOpenChange={(next) => !next && onClose()}>
            <RDialog.Portal>
                <RDialog.Overlay className="fixed inset-0 z-50 bg-scrim/60 backdrop-blur-[2px]" />
                <RDialog.Content
                    data-testid="model-setup"
                    onInteractOutside={(e) => e.preventDefault()}
                    className={cn(
                        'fixed left-1/2 top-[8vh] z-50 flex max-h-[84vh] w-[calc(100vw-2rem)] max-w-3xl',
                        '-translate-x-1/2 flex-col rounded-xl border border-border-strong bg-surface shadow-elevated focus:outline-none',
                    )}
                >
                    <header className="flex items-start justify-between gap-4 px-5 pt-6 sm:px-8 sm:pt-7">
                        <div className="min-w-0">
                            <p className="text-2xs font-medium uppercase tracking-wide text-text-faint">
                                Connect a model · {index + 1} of 2
                            </p>
                            <RDialog.Title className="mt-2 text-balance text-2xl font-semibold tracking-tight text-text">
                                {step === 'providers' ? 'Which AI should Synapse use?' : 'Pick your two models'}
                            </RDialog.Title>
                            <RDialog.Description className="mt-1.5 text-sm text-text-muted">
                                {step === 'providers'
                                    ? 'Pick what you have. A model CLI or Ollama on this computer needs no key; a provider’s key goes into its own card.'
                                    : 'One runs your workflows and agents every day; the other is Synapse AI’s, the assistant that builds them.'}
                            </RDialog.Description>
                        </div>
                        <RDialog.Close
                            aria-label="Close"
                            className={cn(buttonStyles({ variant: 'ghost', size: 'sm', iconOnly: true }), '-mr-2 -mt-1 text-text-faint')}
                        >
                            <X className="size-4" aria-hidden />
                        </RDialog.Close>
                    </header>

                    <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-6 sm:px-8">
                        <ErrorNote>{error}</ErrorNote>
                        {!providers && !error && <p className="text-sm text-text-faint">Reading the models this install can reach…</p>}

                        {providers && step === 'providers' && (
                            <>
                                {problem && (
                                    <p className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5 text-xs leading-relaxed text-text" data-model-problem>
                                        <strong className="font-semibold">Synapse can’t reach its model yet.</strong> <WithCode text={problem} />
                                    </p>
                                )}

                                {found.length > 0 && (
                                    <FoundHereList found={found} checking={checking} onCheck={() => void checkAgain()} />
                                )}

                                {empty && (
                                    <TwoWays path={path} checking={checking}
                                        onKey={() => { setPath('key'); if (!picked.includes(FREE_KEY_PROVIDER)) toggle(FREE_KEY_PROVIDER); }}
                                        onOllama={() => setPath('ollama')} onCheck={() => void checkAgain()} />
                                )}

                                <section className="space-y-2.5">
                                    <h3 className="text-sm font-semibold text-text">
                                        {empty ? 'Or a key you already have' : 'A provider’s key'}
                                    </h3>
                                    <div role="group" aria-label="Providers you have a key for" className="grid gap-2 sm:grid-cols-3">
                                        {shown.map(key => {
                                            const pressed = picked.includes(key);
                                            const note = keyNote(secrets[secretOf(key)!], providers[key]);
                                            return (
                                                <button key={key} type="button" onClick={() => toggle(key)} aria-pressed={pressed}
                                                    data-setup-provider={key} className={tile(pressed)}>
                                                    <AppMark name={providerLabel(key)} imgSrc={PROVIDER_META[key]?.img} size="sm" />
                                                    <span className="min-w-0 flex-1">
                                                        <span className="block truncate text-sm font-medium text-text">{providerLabel(key)}</span>
                                                        {note === 'saved' && <span className="block text-2xs text-text-muted">Key saved</span>}
                                                        {note === 'refused' && <span className="block text-2xs text-warning">Key refused</span>}
                                                    </span>
                                                    {pressed && <Check className="size-4 shrink-0 text-accent" aria-hidden />}
                                                </button>
                                            );
                                        })}
                                    </div>
                                    {!more && (
                                        <button type="button" onClick={() => setMore(true)}
                                            className={cn('rounded text-sm font-medium text-accent hover:underline', focusRing)}>
                                            More providers
                                        </button>
                                    )}
                                </section>

                                {picked.length > 0 && (
                                    <div className="space-y-2">
                                        <p className="text-xs text-text-muted">
                                            Paste each key and press Save. It stays in this install, and you can change it later in Settings → Models.
                                        </p>
                                        {picked.map(key => {
                                            const name = secretOf(key)!;
                                            const info = providers[key] ?? EMPTY;
                                            const stored = secrets[name];
                                            return (
                                                <ProviderCard
                                                    key={key}
                                                    meta={cardMeta(key)}
                                                    {...cardStatus(key, info)}
                                                    error={info.error}
                                                    keyState={stored ? { set: stored.set, hint: stored.hint ? `…${stored.hint}` : null } : undefined}
                                                    keyLabel={KEY_LABELS[key]}
                                                    onSaveKey={(v: string) => afterKey(() => postSecret(name, v))}
                                                    onRemoveKey={() => afterKey(() => clearSecret(name))}
                                                    models={info.models}
                                                    open={!collapsed.includes(key)}
                                                    onToggle={() => setCollapsed(c => (c.includes(key) ? c.filter(k => k !== key) : [...c, key]))}
                                                />
                                            );
                                        })}
                                    </div>
                                )}
                                <p className="text-xs text-text-faint">
                                    Bedrock, or a model on your own server? Set those up in Settings → Models after this.
                                </p>
                            </>
                        )}

                        {providers && step === 'models' && (
                            options.length === 0 ? (
                                <p className="text-sm text-text-muted" data-no-models>
                                    No model can be picked yet. Go back and save a key, sign in to a CLI, or start Ollama.
                                </p>
                            ) : (
                                <div className="space-y-5">
                                    <Field label="Everyday model" hint="Workflows, agents and chat run on it unless they name their own.">
                                        <Combobox value={everyday || undefined} onChange={setEveryday} options={options}
                                            placeholder="Select a model…" searchPlaceholder={SEARCH} aria-label="Everyday model" />
                                    </Field>
                                    <Field label="Synapse AI’s model" hint="Builds your workflows and answers in the assistant. A stronger model pays for itself here.">
                                        <Combobox value={assistant || 'same'} onChange={(v) => setAssistant(v === 'same' ? SAME : v)}
                                            options={[{ value: 'same', label: 'Same as everyday model' }, ...options]}
                                            searchPlaceholder={SEARCH} aria-label="Synapse AI’s model" />
                                    </Field>
                                    <p className="text-xs text-text-faint">You can change both in Settings → Models.</p>
                                </div>
                            )
                        )}
                    </div>

                    <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-5 py-4 sm:px-8">
                        <ol className="mr-auto flex items-center gap-1.5" aria-label={`Step ${index + 1} of 2`}>
                            {[0, 1].map(i => (
                                <li key={i} className={cn('h-1.5 rounded-full transition-all',
                                    i === index ? 'w-6 bg-accent' : i < index ? 'w-1.5 bg-accent/50' : 'w-1.5 bg-border-strong')} />
                            ))}
                        </ol>
                        <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>Don&rsquo;t show again</Button>
                        {step === 'models' && <Button variant="secondary" onClick={() => setStep('providers')} disabled={busy}>Back</Button>}
                        {step === 'providers'
                            ? <Button variant="primary" onClick={toModels} disabled={!providers}>Continue</Button>
                            : <Button variant="primary" onClick={() => void save()} loading={busy} disabled={!everyday}>Done</Button>}
                    </footer>
                </RDialog.Content>
            </RDialog.Portal>
        </RDialog.Root>
    );
}

/** A sentence from the backend with its `commands` shown as code (as Chat's model line does). */
function WithCode({ text }: { text: string }) {
    return (
        <>
            {text.split('`').map((part, i) => (i % 2
                ? <code key={i} className="font-code text-text">{part}</code>
                : <span key={i}>{part}</span>))}
        </>
    );
}

/** What answers here without a key: each CLI's state in its own words, and Ollama's models. */
function FoundHereList({ found, checking, onCheck }: { found: FoundHere[]; checking: boolean; onCheck: () => void }) {
    return (
        <section className="space-y-2.5" data-found-here>
            <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-semibold text-text">Found on this computer, no key needed</h3>
                <Button variant="ghost" size="sm" onClick={onCheck} loading={checking}>
                    <RefreshCw className="size-3.5" aria-hidden /> Check again
                </Button>
            </div>
            <ul className="grid gap-2 sm:grid-cols-2">
                {found.map(f => (
                    <li key={f.key} data-found={f.key} data-state={f.state}
                        className="flex items-start gap-3 rounded-lg border border-border bg-surface px-3 py-2.5">
                        <AppMark name={providerLabel(f.key)} imgSrc={PROVIDER_META[f.key]?.img} size="sm"
                            imgClassName={cardMeta(f.key).iconClassName} />
                        <span className="min-w-0 flex-1">
                            <span className="block truncate text-sm font-medium text-text">{providerLabel(f.key)}</span>
                            {f.state === 'ready' ? (
                                <span className="block text-2xs text-text-muted">
                                    {f.key === 'ollama' ? 'Running' : 'Signed in'} · {f.models} model{f.models !== 1 ? 's' : ''}
                                </span>
                            ) : (
                                <span className="block text-2xs leading-relaxed text-warning">
                                    Not signed in. <span className="text-text-muted"><WithCode text={f.hint ?? ''} /> Then press Check again.</span>
                                </span>
                            )}
                        </span>
                    </li>
                ))}
            </ul>
        </section>
    );
}

/** With nothing at all: two ways to start, each with what it costs (Naveen, 2026-10-20: a free key first). */
function TwoWays({ path, checking, onKey, onOllama, onCheck }: {
    path: 'key' | 'ollama' | null; checking: boolean; onKey: () => void; onOllama: () => void; onCheck: () => void;
}) {
    return (
        <section className="space-y-2.5" data-two-ways>
            <h3 className="text-sm font-semibold text-text">Nothing on this computer yet. Two ways to start:</h3>
            <div role="radiogroup" aria-label="How to start" className="grid gap-2 sm:grid-cols-2">
                <button type="button" role="radio" aria-checked={path === 'key'} onClick={onKey} data-way="key"
                    className={cn(tile(path === 'key'), 'items-start p-3')}>
                    <AppMark name="Google Gemini" imgSrc={PROVIDER_META.gemini?.img} size="sm" />
                    <span className="min-w-0">
                        <span className="block text-sm font-medium text-text">A free Gemini key</span>
                        <span className="mt-0.5 block text-xs text-text-muted">
                            A few minutes at Google AI Studio, and good with tools. Your prompts go to Google.
                        </span>
                    </span>
                </button>
                <button type="button" role="radio" aria-checked={path === 'ollama'} onClick={onOllama} data-way="ollama"
                    className={cn(tile(path === 'ollama'), 'items-start p-3')}>
                    <AppMark name="Ollama" imgSrc={OLLAMA_IMG} imgClassName="theme-adaptive-icon" size="sm" />
                    <span className="min-w-0">
                        <span className="block text-sm font-medium text-text">Ollama on this computer</span>
                        <span className="mt-0.5 block text-xs text-text-muted">
                            Private, no account. A download and a capable machine; small local models build workflows poorly.
                        </span>
                    </span>
                </button>
            </div>
            {path === 'ollama' && (
                <ol className="list-decimal space-y-1.5 rounded-md bg-surface-2 py-3 pl-8 pr-4 text-xs leading-relaxed text-text-muted" data-ollama-steps>
                    <li>
                        <a href="https://ollama.com/download" target="_blank" rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 font-medium text-accent-ink hover:underline">
                            Download Ollama <ExternalLink className="size-3" aria-hidden />
                        </a> and start it.
                    </li>
                    <li>In a terminal, pull a model: <code className="font-code text-text">ollama pull qwen3</code></li>
                    <li>
                        Press{' '}
                        <button type="button" onClick={onCheck} disabled={checking}
                            className={cn('rounded font-medium text-accent-ink hover:underline', focusRing)}>
                            {checking ? 'Checking…' : 'Check again'}
                        </button>
                        ; its models then show above.
                    </li>
                </ol>
            )}
        </section>
    );
}
