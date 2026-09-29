'use client';

/**
 * The images a chat message carried: thumbnails that open full size (F238).
 *
 * A thumbnail called `window.open` on the image's `data:` URL, and Chrome will
 * not open a data URL in a new tab, so a click (and, since F236, Enter) did
 * nothing. The image opens in a dialog on the page instead: no new tab, no
 * popup blocker, and no second copy of the bytes — the dialog draws the same
 * `data:` URL the thumbnail does.
 */

import { useState } from 'react';

import { Modal } from '@/components/ui';

export function ImageThumbnails({ images }: { images: string[] }) {
  const [open, setOpen] = useState<number | null>(null);
  const name = (i: number) => `Attached image ${i + 1}`;
  return (
    <>
      <div className="flex flex-wrap gap-3 mt-4">
        {images.map((img, i) => (
          <div key={i} className="relative group">
            {/* A button, so a keyboard opens the image as a click does (F236). */}
            <button
              type="button"
              onClick={() => setOpen(i)}
              aria-label={`Open ${name(i).toLowerCase()}`}
              className="block rounded-xl"
            >
              <img
                src={img}
                alt=""
                className="h-[4.5rem] w-[4.5rem] object-cover border border-zinc-700/60 rounded-xl hover:border-zinc-400 hover:scale-[1.03] hover:shadow-lg transition-all"
              />
            </button>
          </div>
        ))}
      </div>
      <Modal open={open !== null} onClose={() => setOpen(null)} title={name(open ?? 0)} size="lg">
        {open !== null && (
          <img
            src={images[open]}
            alt={name(open)}
            className="mx-auto max-h-[65vh] max-w-full rounded-md object-contain"
          />
        )}
      </Modal>
    </>
  );
}
