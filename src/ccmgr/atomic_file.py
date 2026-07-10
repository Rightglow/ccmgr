"""Small atomic-file helpers for ccmgr-owned state."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Replace *path* atomically after writing *text* beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    tmp = Path(raw_tmp)
    stream = None
    try:
        stream = os.fdopen(fd, "w", encoding=encoding)
        fd = -1
        with stream:
            stream.write(text)
        stream = None
        os.replace(tmp, path)
    finally:
        if stream is not None:
            stream.close()
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
