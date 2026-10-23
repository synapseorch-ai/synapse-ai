"use client";

/**
 * One provider on Settings → Models, the same card in both products (PROV1, D245).
 *
 * Cloud's card, moved here and synced like `components/run`, so OSS reads as
 * cloud does (Naveen). It shows its state before you open it:
 *
 *   · a lit status dot beside the words: a model count, "Key saved", or why not
 *   · the brand mark in colour, greyscale and dimmed when not usable
 *   · the card lifting off the page, and its name at full strength, when live
 *   · the saved key's fragment on the right, so you can tell which key is in place
 *
 * Opened, it holds the key field with its own Save and a Remove that asks
 * first, then anything else the provider needs (`extras`: Bedrock's region, a
 * server's address, a CLI's model names), then the models it offers. What a
 * save or a remove calls is each product's own: cloud's provider-keys routes,
 * OSS's settings.
 */

import { AlertTriangle, Check, ChevronDown, ExternalLink, Eye, EyeOff, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { AppMark, Button, ConfirmDialog, ErrorNote, Field, IconButton, Input } from "@/components/ui";
import { cn } from "@/lib/cn";

export type ProviderCardMeta = {
  label: string;
  /** A logo of ours; none is a theme-aware letter tile (`AppMark`). */
  icon?: string;
  /** A monochrome logo that inverts on the dark ground. */
  iconClassName?: string;
  description: string;
  keyPlaceholder?: string;
  keyLink?: { label: string; url: string };
  keyNote?: string;
  /** A caution shown above the key, in the warning tone. */
  warning?: string;
  /** Why paste a key at all, under the fields. */
  connectNote?: string;
};

/** A stored key: whether one is in place, and the fragment that says which. */
export type ProviderKeyState = { set: boolean; hint: string | null };

/** The most models listed as chips; past it the rest are counted. OpenRouter alone offers hundreds. */
export const MODEL_CHIPS = 40;

export function ProviderCard({
  meta,
  live,
  status,
  error,
  keyState,
  keyLabel = "API key",
  canEdit = true,
  onSaveKey,
  onRemoveKey,
  removeNote = "Runs using it will start failing.",
  extras,
  models = [],
  modelsCached = false,
  open: openProp,
  onToggle,
}: {
  meta: ProviderCardMeta;
  /** Usable now: a key saved, a server running, models found. */
  live: boolean;
  /** Beside the dot: "3 models", "No key configured", "Key saved", "Not running". */
  status: string;
  /** Why its models could not be read, said in place of the status. */
  error?: string | null;
  keyState?: ProviderKeyState;
  keyLabel?: string;
  canEdit?: boolean;
  /** Saves a key typed into this card. Absent: the card has no key field (a CLI, Ollama). */
  onSaveKey?: (value: string) => Promise<void>;
  onRemoveKey?: () => Promise<void>;
  /** What removing the key does, in the dialog that asks. */
  removeNote?: string;
  /** The provider's other fields, below the key. */
  extras?: ReactNode;
  models?: string[];
  /** The list is the last one read, not a fresh one. */
  modelsCached?: boolean;
  /** Controlled open state; left out, the card keeps its own. */
  open?: boolean;
  onToggle?: () => void;
}) {
  const [ownOpen, setOwnOpen] = useState(false);
  const open = openProp ?? ownOpen;
  const toggle = onToggle ?? (() => setOwnOpen((v) => !v));
  const [value, setValue] = useState("");
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [confirmingRemove, setConfirmingRemove] = useState(false);
  const saved = keyState?.set === true;
  // A card that takes a key and has none says so before it is opened, and opening it puts the cursor in the key
  // box (GUIDE1, F912: "it's not that evident where to add the key").
  const wantsKey = Boolean(onSaveKey) && canEdit && !saved && !live;
  const keyInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (open && wantsKey) keyInput.current?.focus();
  }, [open, wantsKey]);

  async function save() {
    if (!onSaveKey) return;
    setProblem("");
    setBusy(true);
    try {
      await onSaveKey(value.trim());
      setValue("");
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "Could not save that key.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!onRemoveKey) return;
    setConfirmingRemove(false);
    setBusy(true);
    try {
      await onRemoveKey();
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "Could not remove that key.");
    } finally {
      setBusy(false);
    }
  }

  const shown = models.slice(0, MODEL_CHIPS);

  return (
    <div
      data-provider-card={meta.label}
      className={cn(
        "rounded-md border transition-colors",
        live ? "border-border-strong bg-surface" : "border-border bg-bg",
      )}
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-row-hover"
      >
        <span className="flex min-w-0 items-center gap-3">
          <AppMark
            name={meta.label}
            imgSrc={meta.icon}
            imgClassName={meta.iconClassName}
            size="sm"
            className={cn(!live && "opacity-50 grayscale")}
          />
          <span className="min-w-0">
            <span className={cn("text-sm font-medium", live ? "text-text" : "text-text-faint")}>{meta.label}</span>
            <span className="ml-2 inline-flex items-center gap-1.5 text-xs text-text-faint">
              {/* Quiet status: a dot beside the words, no glow (UX1). */}
              <span aria-hidden className={cn("size-1.5 rounded-full", live ? "bg-success" : "bg-text-faint")} />
              {error || status}
            </span>
          </span>
        </span>

        <span className="flex shrink-0 items-center gap-2">
          {error && <AlertTriangle className="size-3.5 text-warning" aria-hidden />}
          {wantsKey && !open && (
            <span className="rounded-md border border-border-strong bg-surface px-2 py-0.5 text-xs font-medium text-text">
              Add key
            </span>
          )}
          {saved && keyState?.hint && (
            <span className="font-mono text-2xs text-text-muted" title="A fragment of the stored key">
              {keyState.hint}
            </span>
          )}
          <ChevronDown
            className={cn("size-4 text-text-faint transition-transform", open && "rotate-180")}
            aria-hidden
          />
        </span>
      </button>

      {open && (
        <div className="space-y-4 border-t border-border px-4 pb-4 pt-3">
          <p className="text-xs leading-relaxed text-text-faint">{meta.description}</p>

          {meta.warning && (
            <p className="flex items-start gap-2 rounded-md border border-warning/25 bg-warning-subtle px-3 py-2.5 text-xs leading-relaxed text-text">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
              {meta.warning}
            </p>
          )}

          {/*
            A stored key reads as a missing one unless the empty box is
            explained: keys are write-only, so an empty input under a heading
            looked like a field that failed to load (Naveen read it that way,
            with a working key and forty models listed above). The stored key
            gets its own line, and the box says what it is for.
          */}
          {onSaveKey && saved && (
            <p className="flex items-center gap-2 text-xs text-text-muted">
              <Check className="size-3.5 shrink-0 text-success" aria-hidden />
              A key is stored
              {keyState?.hint && <span className="font-mono text-text">{keyState.hint}</span>}
              <span className="text-text-faint">· never shown again, even to you</span>
            </p>
          )}

          {onSaveKey && canEdit && (
            <Field
              label={saved ? "Replace it" : keyLabel}
              hint={
                <>
                  {meta.keyNote}
                  {meta.keyNote && meta.keyLink && " "}
                  {meta.keyLink && (
                    <a
                      href={meta.keyLink.url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-accent-ink hover:underline"
                    >
                      {meta.keyLink.label}
                      <ExternalLink className="size-3" aria-hidden />
                    </a>
                  )}
                </>
              }
            >
              <span className="flex gap-2">
                <span className="relative flex-1">
                  <Input
                    ref={keyInput}
                    type={visible ? "text" : "password"}
                    value={value}
                    autoComplete="off"
                    placeholder={saved ? "Paste a new key to replace it" : meta.keyPlaceholder}
                    aria-label={keyLabel.includes(meta.label) ? keyLabel : `${meta.label} ${keyLabel}`}
                    className="pr-9"
                    onChange={(e) => setValue(e.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setVisible((v) => !v)}
                    aria-label={visible ? "Hide the key" : "Show the key"}
                    className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-text-faint hover:text-text"
                  >
                    {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </span>
                <Button onClick={() => void save()} loading={busy} disabled={!value.trim()}>
                  Save
                </Button>
                {saved && onRemoveKey && (
                  <IconButton
                    label={`Remove the ${meta.label} key`}
                    icon={Trash2}
                    tone="danger"
                    onClick={() => setConfirmingRemove(true)}
                    disabled={busy}
                  />
                )}
              </span>
            </Field>
          )}

          <ErrorNote>{problem}</ErrorNote>

          {extras}

          {meta.connectNote && (
            <p className="border-t border-border pt-3 text-2xs leading-relaxed text-text-faint">
              <span className="font-medium text-text-muted">Why paste a key? </span>
              {meta.connectNote}
            </p>
          )}

          {live && models.length > 0 && (
            <div className="space-y-1.5">
              <p className="text-xs text-text-muted">
                Available models{modelsCached ? " (cached)" : ""}
                {models.length > shown.length && ` · ${models.length}`}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {shown.map((m) => (
                  <span
                    key={m}
                    className="rounded-sm border border-border bg-surface-2 px-2 py-0.5 font-mono text-2xs text-text-muted"
                  >
                    {m}
                  </span>
                ))}
                {models.length > shown.length && (
                  <span className="px-1 py-0.5 text-2xs text-text-faint">
                    and {models.length - shown.length} more, in every model picker
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirmingRemove}
        onClose={() => setConfirmingRemove(false)}
        onConfirm={() => void remove()}
        busy={busy}
        title={`Remove the ${meta.label} key?`}
        confirmLabel="Remove"
        description={removeNote}
      />
    </div>
  );
}
