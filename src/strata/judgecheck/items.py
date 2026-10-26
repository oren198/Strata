"""The bundled set: loading, and refusing a tampered copy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

SET_V1_SHA256 = "4c16285156a504d4163e73529c9ddb447fd3200486a88a31b1d74081d1c50617"
"""sha256 of `strata/_judgecheck/set-v1.jsonl`. A run refuses when the file differs."""

SUITES = ("h2", "drift", "refinement", "cx", "p5", "j3", "a2")
SET_V1_PATH_NAME = "set-v1.jsonl"


class SetIntegrityError(RuntimeError):
    """The set file is not the one this build was released with."""


@dataclass(frozen=True)
class Item:
    id: str
    suite: str
    data: dict[str, Any]

    @property
    def golden(self) -> dict[str, Any]:
        return self.data["golden"]


def _bundled_bytes() -> bytes:
    return resources.files("strata._judgecheck").joinpath(SET_V1_PATH_NAME).read_bytes()


def load_set(path: str | Path | None = None, *, expected_sha256: str = SET_V1_SHA256) -> list[Item]:
    """The items of the set, in file order.

    With no *path* the copy bundled in the package is read. In either case its sha256 must equal
    *expected_sha256* (the constant above for a normal run): a tampered or edited set is refused,
    never run. Raises :class:`SetIntegrityError`.
    """
    raw = Path(path).read_bytes() if path is not None else _bundled_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise SetIntegrityError(
            f"judge-check set sha256 {digest} does not match the {expected_sha256} this build "
            "was released with; refusing to run."
        )
    items: list[Item] = []
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        suite = row.get("judgecheck_suite")
        if suite not in SUITES:
            raise SetIntegrityError(f"item {row.get('id')!r} names an unknown suite {suite!r}.")
        items.append(Item(id=row["id"], suite=suite, data=row))
    if len({i.id for i in items}) != len(items):
        raise SetIntegrityError("the set has duplicate item ids.")
    return items
