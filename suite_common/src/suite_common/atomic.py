"""Crash-safe atomic file writes.

A forensic deliverable that's half-written on disk after a power loss is
worse than a missing file: the operator might mistake the truncated
artifact for a successful export and hand it over. Every persistent
write in the suite should go through this helper so the on-disk file
is either the prior version or the new version, never a corrupt mix.

The strategy is the standard temp-file + rename dance, plus a pair of
fsyncs to defend against the OS page cache:

1. Open a sibling ``.tmp`` file, write the body, ``fsync`` its file
   descriptor. After this returns, the data is on storage even if the
   machine loses power.
2. ``Path.replace`` atomically swings the final pathname at the new
   contents. POSIX guarantees this is atomic relative to other
   readers; Windows uses ``ReplaceFileW`` under the hood with the
   same guarantee.
3. On POSIX, ``fsync`` the parent directory so the rename's metadata
   change reaches storage. Windows has no equivalent and doesn't
   need one -- NTFS metadata journaling already covers this.

If anything raises before step 2, the ``.tmp`` is best-effort cleaned
up so the next write starts from a clean slate instead of accumulating
crashed-write debris.

Use ``atomic_write_text`` for UTF-8 / configurable encoding payloads
(JSON, HTML, Markdown). For binary payloads, add an ``atomic_write_bytes``
that mirrors the same shape -- we don't expose one yet because every
current caller is text.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def atomic_write_text(
    destination: Path,
    body: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write ``body`` to ``destination`` atomically + durably.

    A crash, ``Ctrl+C``, or power loss at any point leaves the
    destination either at its prior contents (write didn't finish) or
    the new contents (write finished). Never a truncated mix.

    ``destination`` must already exist as a path (its parent directory
    must exist); the caller is responsible for ``mkdir(parents=True)``
    where needed. This matches every existing call site and avoids
    silently creating intermediate directories the writer didn't
    explicitly ask for.
    """
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    # Drop any leftover .tmp from a prior crashed write before we
    # start. write_text would happily overwrite, but doing it
    # explicitly makes the intent obvious and stops repeated crashes
    # from accumulating junk siblings.
    tmp.unlink(missing_ok=True)
    try:
        # Open + write + fsync + close, all on the same fd. Path's
        # write_text closes the file after writing but doesn't fsync,
        # so we hand-roll the open/write/fsync sequence here.
        with tmp.open("w", encoding=encoding, newline="") as fh:
            fh.write(body)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                # fsync can fail on certain filesystems (some
                # network mounts, /dev/shm). Log and continue --
                # the rename below is still atomic, just not durable.
                logger.debug("fsync(%s) failed: %s; continuing without durability", tmp, exc)
        tmp.replace(destination)
    except BaseException:
        # Best-effort cleanup so a partial .tmp doesn't survive.
        # Catches Ctrl+C (KeyboardInterrupt) as well as OSError so an
        # interrupted write also leaves a clean directory; we re-raise
        # the original after cleanup.
        tmp.unlink(missing_ok=True)
        raise
    # Step 3: durability of the rename itself. POSIX-only -- Windows
    # doesn't expose a directory fsync, and NTFS metadata journaling
    # covers this for us. Best-effort: if the directory open fails
    # (e.g. EISDIR semantics on a weird filesystem) we accept the
    # rename's atomicity without the durability guarantee.
    if os.name == "posix":
        try:
            dir_fd = os.open(str(destination.parent), os.O_RDONLY)
        except OSError as exc:
            logger.debug(
                "open(%s, O_RDONLY) failed: %s; rename atomic but not durable",
                destination.parent,
                exc,
            )
            return
        try:
            try:
                os.fsync(dir_fd)
            except OSError as exc:
                logger.debug("fsync(%s) failed: %s", destination.parent, exc)
        finally:
            os.close(dir_fd)
