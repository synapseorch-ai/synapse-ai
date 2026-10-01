"""Opening an Office, OpenDocument or EPUB zip without letting it expand without bound.

Checked before any reader touches the file: the member count, the total the
members declare, and each member's ratio. Python's `zipfile` never reads past
a member's declared size (a stream that runs longer fails its CRC), so the
declared sizes bound what a reader can be made to decompress — including
openpyxl, which opens the zip itself. Overlapping members (one compressed run
claimed by many entries) each declare their size and count toward the total.
"""
from __future__ import annotations

import io
import zipfile

from core.knowledge.parse import limits
from core.knowledge.parse.limits import Encrypted, ExpandsTooFar, ParseRefused


class SafeZip:
    def __init__(self, data: bytes, *, kind: str = "file"):
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError) as exc:
            raise ParseRefused(f"The {kind} is damaged: {exc}.") from exc
        infos = self._zip.infolist()
        if len(infos) > limits.ZIP_MAX_MEMBERS:
            raise ExpandsTooFar(f"The {kind} holds {len(infos)} parts; the limit is {limits.ZIP_MAX_MEMBERS}.")
        total = sum(i.file_size for i in infos)
        if total > limits.ZIP_MAX_EXPANDED:
            raise ExpandsTooFar(f"The {kind} expands to {total / 2**20:.0f} MB; the limit is "
                                f"{limits.ZIP_MAX_EXPANDED / 2**20:.0f} MB.")
        for info in infos:
            ratio = info.file_size // max(info.compress_size, 1)
            if info.file_size > limits.ZIP_RATIO_FLOOR and ratio > limits.ZIP_MAX_RATIO:
                raise ExpandsTooFar(f"A part of the {kind} ({info.filename}) expands {ratio}× — more than a "
                                    "real document does.")
            if info.flag_bits & 0x1:
                raise Encrypted(f"The {kind} is protected by a password.")
        self._infos = {i.filename: i for i in infos}

    def names(self) -> list[str]:
        return list(self._infos)

    def has(self, name: str) -> bool:
        return name in self._infos

    def read(self, name: str) -> bytes:
        try:
            return self._zip.read(name)
        except (zipfile.BadZipFile, KeyError, EOFError) as exc:
            raise ParseRefused(f"The part {name} is damaged: {exc}.") from exc

    def read_text(self, name: str) -> str:
        return self.read(name).decode("utf-8", errors="replace")

    def size(self, name: str) -> int:
        return self._infos[name].file_size
