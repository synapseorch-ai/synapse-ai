"""
The images a run started with, kept while it waits (F395).

A workflow agent's run gets the images chat was sent (F313), and a step with
"Sees attachments" reads them. They ride on the engine (`engine.images`), never
in shared state, which is echoed to the browser and checkpointed at every step.
So a run that paused on a person lost them: the resume built a new engine, and
a step after the answer saw only what the answer carried.

Now a top-level run keeps **references** to its images on the run, beside its
time and failure reason (`step_progress["__run__"]["images"]`: a few short
strings, never the bytes), and every resume reads them back. Where the bytes
live is the host's:

- **This install** (the default): each image is a blob under the run,
  `runs/<run_id>/images/<n>`, in the tenant's blob store; the keys are the
  references. Forgotten when the run completes (nothing resumes it) or its log
  is deleted; a failed or cancelled run keeps them for a Replay.
- **A host that already stores them** (cloud: chat's attachments) passes its
  own references with the images, and installs a `RunImages` that turns them
  back into images and forgets nothing — they have a life of their own.
"""
from __future__ import annotations

from typing import Protocol

from core.log import get_logger

_log = get_logger(__name__)

#: The key under the run's own `step_progress` entry (`engine.ACTIVE_TIME_KEY`).
REFS_KEY = "images"


class RunImages(Protocol):
    async def keep(self, run_id: str, images: list[str], refs: list[str] | None) -> list[str]:
        """Store `images` for `run_id` (or adopt the caller's `refs`); the references to keep on the run."""
        ...

    async def load(self, run_id: str, refs: list[str]) -> list[str]:
        """The images `refs` name, as data URIs, in order; one that is gone is left out."""
        ...

    async def forget(self, run_id: str) -> None:
        """Let go of what `keep` stored for this run."""
        ...


def _prefix(run_id: str) -> str:
    return f"runs/{run_id}/images/"


class BlobRunImages:
    """Each image a blob in the tenant's store, under its run."""

    async def keep(self, run_id: str, images: list[str], refs: list[str] | None) -> list[str]:
        if refs:
            return list(refs)
        from core.storage import get_blob_store

        store = get_blob_store()
        keys = []
        for index, image in enumerate(images):
            key = f"{_prefix(run_id)}{index}"
            store.put(key, image)
            keys.append(key)
        return keys

    async def load(self, run_id: str, refs: list[str]) -> list[str]:
        from core.storage import get_blob_store

        store = get_blob_store()
        # Only this run's own: a reference is read back from the run's record,
        # and never names another run's blobs.
        return [image for key in refs if key.startswith(_prefix(run_id)) and (image := store.get(key))]

    async def forget(self, run_id: str) -> None:
        from core.storage import get_blob_store

        store = get_blob_store()
        for key in store.list(_prefix(run_id)):
            store.delete(key)


_impl: RunImages = BlobRunImages()


def set_run_images(impl: RunImages | None) -> None:
    """Install the host's `RunImages`. None restores the blob-store default."""
    global _impl
    _impl = impl if impl is not None else BlobRunImages()


async def keep(run_id: str, images: list[str], refs: list[str] | None = None) -> list[str]:
    """References for the run's images, or [] when it has none or they could not be kept.

    Never fails the run: an image that cannot be kept is lost to the steps after
    a pause, as every image was before F395, and the log says so.
    """
    if not images:
        return []
    try:
        return await _impl.keep(run_id, list(images), list(refs) if refs else None)
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"run {run_id}: its images were not kept for after a pause: {exc}")
        return []


async def load(run_id: str, refs: list[str] | None) -> list[str]:
    """The run's images back, as data URIs; [] when it kept none."""
    if not refs:
        return []
    try:
        images = await _impl.load(run_id, list(refs))
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"run {run_id}: its images could not be read back: {exc}")
        return []
    if len(images) < len(refs):
        _log.warning(f"run {run_id}: {len(refs) - len(images)} of its {len(refs)} images are gone; "
                     "the steps after the pause see the rest")
    return images


async def forget(run_id: str) -> None:
    try:
        await _impl.forget(run_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"run {run_id}: its kept images were not removed: {exc}")


def joined(kept: list[str], sent: list[str] | None) -> list[str]:
    """The run's images, then any the answer brought that it did not already have."""
    return list(kept) + [image for image in (sent or []) if image not in kept]
