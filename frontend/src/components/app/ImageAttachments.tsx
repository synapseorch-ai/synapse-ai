"use client";
/**
 * Attaching images to a message.
 *
 * All of this was inline in the chat screen, and the builder had none of it —
 * which was the complaint: image upload existed in one place, its UI was poor,
 * and the builder could not do it at all. Rather than write a second copy for
 * the builder, the whole apparatus comes out here: one validator, one chip, one
 * counter, one set of refusal messages.
 *
 * The limits mirror `backend/core/images.py` (`MAX_IMAGES`, `MAX_BYTES`,
 * `ALLOWED_MIME`) — deliberately, and with that module named here, because the
 * last time this limit lived in two places one of them was a comment and a
 * `[:5]` slice, and callers reaching the API directly were bounded by nothing.
 * The server still decides; this only means you find out before the round trip
 * rather than after a failed turn.
 */

import { AlertCircle, Image as ImageIcon, X } from 'lucide-react';
import { useCallback, useRef, useState } from 'react';

import { cn } from '@/lib/cn';

/** Mirrors `core.images.MAX_IMAGES`. */
export const MAX_IMAGES = 5;
/** Mirrors `core.images.MAX_BYTES`. */
const MAX_BYTES = 5 * 1024 * 1024;
/** Mirrors `core.images.ALLOWED_MIME`. */
const ALLOWED = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];

/** What the composer holds and what the request sends. */
export interface Attachment {
    /** A data URI, used both as the thumbnail and as what goes over the wire. */
    preview: string;
    base64: string;
    name: string;
}

export interface ImageAttachments {
    images: Attachment[];
    /** Why the last attempt did not take. Files used to be dropped in silence. */
    error: string | null;
    accept: (files: File[]) => void;
    remove: (index: number) => void;
    clear: () => void;
    dismissError: () => void;
    /** Spread onto whatever element should accept a drop — usually the form. */
    dropHandlers: {
        onDrop: (e: React.DragEvent) => void;
        onDragOver: (e: React.DragEvent) => void;
    };
    /** Spread onto the textarea, so a pasted screenshot attaches. */
    pasteHandlers: { onPaste: (e: React.ClipboardEvent) => void };
}

export function useImageAttachments(): ImageAttachments {
    const [images, setImages] = useState<Attachment[]>([]);
    const [error, setError] = useState<string | null>(null);

    const accept = useCallback((files: File[]) => {
        setError(null);
        setImages((current) => {
            const room = MAX_IMAGES - current.length;
            if (room <= 0) {
                setError(`${MAX_IMAGES} images is the limit for one message.`);
                return current;
            }

            const usable: File[] = [];
            for (const file of files) {
                if (!ALLOWED.includes(file.type)) {
                    setError(`${file.name || 'That file'} is not a PNG, JPEG, GIF or WebP.`);
                    continue;
                }
                if (file.size > MAX_BYTES) {
                    setError(`${file.name || 'That image'} is ${Math.round(file.size / (1024 * 1024))}\u00a0MB; the limit is ${MAX_BYTES / (1024 * 1024)}\u00a0MB.`);
                    continue;
                }
                usable.push(file);
            }
            if (usable.length > room) {
                setError(`Only ${room} more ${room === 1 ? 'image fits' : 'images fit'} in this message.`);
            }

            // Reading is async, so each file appends when it lands rather than
            // this returning them. The cap is re-checked on append: two drops in
            // flight at once would otherwise both see room for the same slot.
            usable.slice(0, room).forEach((file) => {
                const reader = new FileReader();
                reader.onload = (ev) => {
                    const base64 = ev.target?.result as string;
                    setImages((prev) => prev.length < MAX_IMAGES
                        ? [...prev, { preview: base64, base64, name: file.name || 'image' }]
                        : prev);
                };
                reader.readAsDataURL(file);
            });
            return current;
        });
    }, []);

    const remove = useCallback((index: number) => {
        setImages((prev) => prev.filter((_, i) => i !== index));
        setError(null);
    }, []);

    const clear = useCallback(() => { setImages([]); setError(null); }, []);
    const dismissError = useCallback(() => setError(null), []);

    return {
        images, error, accept, remove, clear, dismissError,
        dropHandlers: {
            onDrop: (e) => {
                e.preventDefault();
                const files = Array.from(e.dataTransfer.files);
                if (files.length) accept(files);
            },
            onDragOver: (e) => e.preventDefault(),
        },
        pasteHandlers: {
            onPaste: (e) => {
                const files = Array.from(e.clipboardData.files);
                if (files.length) { e.preventDefault(); accept(files); }
            },
        },
    };
}

/** Why the last attach did not take, with a way to dismiss it. */
export function AttachmentError({ error, onDismiss }: { error: string | null; onDismiss: () => void }) {
    if (!error) return null;
    return (
        <div
            role="status"
            aria-live="polite"
            className="mb-2 flex items-center gap-2 rounded-md border border-warning/50 bg-warning-subtle px-3 py-2 text-[12px] text-warning"
        >
            <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />
            <span className="min-w-0 flex-1 font-sans">{error}</span>
            <button
                type="button"
                onClick={onDismiss}
                aria-label="Dismiss"
                className="shrink-0 rounded p-0.5 opacity-70 transition-opacity hover:opacity-100"
            >
                <X className="h-3 w-3" aria-hidden />
            </button>
        </div>
    );
}

/**
 * The attached files, as chips inside the composer's border.
 *
 * They belong to the message being written; a separate bar above the composer
 * read as a notification about them. The remove button is always visible and
 * focusable — the old one appeared on hover, which meant it did not exist for a
 * keyboard or a touchscreen.
 */
export function AttachmentChips({
    images,
    onRemove,
}: {
    images: Attachment[];
    onRemove: (index: number) => void;
}) {
    if (!images.length) return null;
    return (
        <div className="flex flex-wrap items-center gap-2 px-2 pb-1 pt-2">
            {images.map((image, index) => (
                <span
                    key={`${image.name}-${index}`}
                    className="flex max-w-[14rem] items-center gap-2 rounded-md border border-border bg-surface-2 py-1 pl-1 pr-1.5"
                >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                        src={image.preview}
                        alt=""
                        width={24}
                        height={24}
                        className="h-6 w-6 shrink-0 rounded object-cover"
                    />
                    <span className="min-w-0 truncate font-sans text-[11px] text-text-muted">{image.name}</span>
                    <button
                        type="button"
                        onClick={() => onRemove(index)}
                        aria-label={`Remove ${image.name}`}
                        className="shrink-0 rounded p-0.5 text-text-faint transition-colors hover:bg-surface hover:text-text focus-visible:text-text"
                    >
                        <X className="h-3 w-3" aria-hidden />
                    </button>
                </span>
            ))}
            <span className="font-sans text-[10px] text-text-faint">
                {images.length} of {MAX_IMAGES}
            </span>
        </div>
    );
}

/**
 * The attach button and the file input behind it.
 *
 * `disabledReason` rather than a bare `disabled`: a button that cannot be
 * pressed and will not say why is the thing people file bugs about. The model
 * not being able to see images is the common case, and it is worth naming —
 * four of the providers discard an attachment with no warning at all.
 */
export function AttachButton({
    onFiles,
    count,
    disabledReason,
    className,
}: {
    onFiles: (files: File[]) => void;
    count: number;
    /** Non-empty disables the button and becomes its tooltip. */
    disabledReason?: string;
    className?: string;
}) {
    const inputRef = useRef<HTMLInputElement>(null);
    const full = count >= MAX_IMAGES;
    const reason = disabledReason || (full ? `${MAX_IMAGES} images is the limit` : '');

    return (
        <>
            <input
                ref={inputRef}
                type="file"
                accept={ALLOWED.join(',')}
                multiple
                className="hidden"
                onChange={(e) => {
                    onFiles(Array.from(e.target.files || []));
                    e.target.value = '';   // so the same file can be picked again
                }}
            />
            <button
                type="button"
                onClick={() => inputRef.current?.click()}
                disabled={Boolean(reason)}
                aria-label="Attach images"
                title={reason || 'Attach images'}
                className={cn(
                    'shrink-0 text-text-faint transition-colors hover:text-text-muted',
                    'disabled:cursor-not-allowed disabled:opacity-30',
                    className,
                )}
            >
                <ImageIcon className="h-[18px] w-[18px]" aria-hidden />
            </button>
        </>
    );
}
