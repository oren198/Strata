"""The fake Hermes ABC is byte-for-byte the pinned source, so the provider is tested against the
real contract and not against a reformatted copy of it."""

from __future__ import annotations

import hashlib
from pathlib import Path

#: sha256 of agent/memory_provider.py in NousResearch/hermes-agent at v0.23.3,
#: commit caafe5389ae585ef77387a962aee05b1bcf17029 (checked with `git show <commit>:<path>`).
PINNED_SHA256 = "46f7073c312fcfdbc9538bdc280b7b180410755a7b0de896d7c2f83410850f05"
HEADER_LINES = 7  # our provenance header; everything after it is the pinned file

COPY = Path(__file__).parent / "fake_hermes" / "agent" / "memory_provider.py"


def test_the_body_after_our_header_has_the_pinned_sha256() -> None:
    data = COPY.read_bytes()
    body = b"".join(data.splitlines(keepends=True)[HEADER_LINES:])
    assert hashlib.sha256(body).hexdigest() == PINNED_SHA256


def test_the_header_names_the_pin_and_the_licence() -> None:
    header = b"".join(COPY.read_bytes().splitlines(keepends=True)[:HEADER_LINES]).decode()
    assert "caafe5389ae585ef77387a962aee05b1bcf17029" in header
    assert "MIT License" in header and "Nous Research" in header
    assert header.startswith("# ---") and header.rstrip().endswith("---")
