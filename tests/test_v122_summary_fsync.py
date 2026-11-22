"""summary_store.write is durable, not only atomic (v1.22 P2, step 1).

``os.replace`` makes the write atomic against a PROCESS crash. After a power loss or an OS crash the
renamed file can still come back empty unless the temp file was fsynced before the rename and the
directory entry after it. "The expected summary version was written" must be a yes/no fact for the
amendment intent journal, so the store fsyncs both, in that order.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from strata import summary_store as module
from strata.summary_store import ScopeSummary, SummaryStore


def _summary() -> ScopeSummary:
    return ScopeSummary(
        scope_id="g_x", directives=[], context="A fact.", updated_at="2026-11-22T00:00:00+00:00"
    )


def test_write_fsyncs_the_temp_file_before_the_rename_and_the_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync:{kind}")
        real_fsync(fd)

    def replace(src, dst):  # noqa: ANN001, ANN202
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(module.os, "replace", replace)

    store = SummaryStore(str(tmp_path / "summaries"))
    written = store.write("g_x", _summary())

    assert events == ["fsync:file", "replace", "fsync:dir"]
    assert store.read("g_x").version == written.version == 1


def test_the_directory_fsync_is_the_summaries_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[str] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    monkeypatch.setattr(module.os, "fsync", fsync)
    root = tmp_path / "summaries"
    SummaryStore(str(root)).write("g_x", _summary())
    if not synced:
        pytest.fail("the directory was not fsynced")
    assert Path(synced[0]).resolve() == root.resolve()
