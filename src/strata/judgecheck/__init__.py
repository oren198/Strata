"""`strata judge-check`, engine side: run a frozen set of judgment items through the engine
with the configured judge and report what was measured.

It is a measurement, not a mode, and it changes nothing in the user's project: every run is
isolated in a temp store. The engine decision of each item is compared with its golden label by
string and field equality, so no second model is involved. Nothing here imports strata-evals; the
scorer is a literal port, held to the original by a cross-check test in that repo.
"""

from strata.judgecheck.bars import Bars
from strata.judgecheck.items import Item, SetIntegrityError, load_set
from strata.judgecheck.scoring import (
    SCHEMA,
    IncompleteInfo,
    ItemResult,
    JudgeCheckScore,
    score,
)

__all__ = [
    "SCHEMA",
    "Bars",
    "IncompleteInfo",
    "Item",
    "ItemResult",
    "JudgeCheckScore",
    "SetIntegrityError",
    "load_set",
    "score",
]
