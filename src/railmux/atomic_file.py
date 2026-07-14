"""Small atomic-file helpers for railmux-owned state."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def migrate_from_ccmgr(path: Path) -> None:
    """One-time config migration for the ccmgr → railmux rename.

    railmux stores state under ``~/.config/railmux/``. Users upgrading from
    the old ``ccmgr`` name have their renames/favorites under
    ``~/.config/ccmgr/`` — orphaned once the package looks under ``railmux``.
    If *path* (a ``.config/railmux/<file>``) doesn't exist yet but the sibling
    ``.config/ccmgr/<file>`` does, copy it over. No-op once the railmux file
    exists, so it runs at most once and never clobbers. The legacy path is
    derived by swapping the ``railmux`` parent dir for ``ccmgr`` (rather than
    from ``Path.home()``), so it only fires for the real config layout and
    leaves tmp-path-backed test stores untouched.
    """
    if path.exists() or path.parent.name != "railmux":
        return
    legacy = path.parent.parent / "ccmgr" / path.name
    if not legacy.is_file():
        return
    try:
        atomic_write_text(path, legacy.read_text())
    except OSError:
        pass


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
