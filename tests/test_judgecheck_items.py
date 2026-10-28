"""The bundled judge-check set: integrity, and that it ships."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from strata.judgecheck import SetIntegrityError, load_set
from strata.judgecheck.items import SET_V1_SHA256

_PACKAGED = Path(__file__).parent.parent / "src" / "strata" / "_judgecheck" / "set-v1.jsonl"


def test_the_bundled_set_matches_the_sha_recorded_in_code() -> None:
    assert hashlib.sha256(_PACKAGED.read_bytes()).hexdigest() == SET_V1_SHA256
    assert len(load_set()) == 78


def test_an_edited_copy_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "set.jsonl"
    shutil.copy(_PACKAGED, copy)
    assert len(load_set(copy)) == 78
    copy.write_bytes(copy.read_bytes().replace(b"decline", b"declinE", 1))
    with pytest.raises(SetIntegrityError, match="does not match"):
        load_set(copy)


def test_a_truncated_or_extended_copy_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "set.jsonl"
    data = _PACKAGED.read_bytes()
    copy.write_bytes(data.rsplit(b"\n", 2)[0] + b"\n")
    with pytest.raises(SetIntegrityError):
        load_set(copy)
    copy.write_bytes(data + b"\n")
    with pytest.raises(SetIntegrityError):
        load_set(copy)


def test_the_check_runs_on_the_tampered_sha_only_when_told_the_matching_sha(tmp_path: Path) -> None:
    copy = tmp_path / "set.jsonl"
    copy.write_bytes(_PACKAGED.read_bytes().replace(b"decline", b"declinE", 1))
    digest = hashlib.sha256(copy.read_bytes()).hexdigest()
    assert len(load_set(copy, expected_sha256=digest)) == 78  # the sha is the only gate
