"""`strata judge-check`, engine side: run a frozen set of judgment items through the engine
with the configured judge and report what was measured.

It is a measurement, not a mode, and it changes nothing in the user's project: every run is
isolated in a temp store. The engine decision of each item is compared with its golden label by
string and field equality, so no second model is involved. Nothing here imports strata-evals; the
scorer is a literal port, held to the original by a cross-check test in that repo.

Entry points (the CLI wires these):

- :func:`load_set` -> ``list[Item]`` (refuses a tampered set).
- :func:`resolve_price` / :func:`estimate` / :func:`item_prompt_chars`: the estimate and price.
- :func:`judge_spec_from_settings` -> :class:`JudgeSpec`; :func:`describe_judge` for the report.
- :func:`run` -> :class:`RunOutcome` (results per judge label, spend, calls, incomplete info).
- :func:`score` -> :class:`JudgeCheckScore`; :func:`compare` for a paired A/B.
"""

from strata.judgecheck.bars import BARS_V1, Bars
from strata.judgecheck.compare import compare
from strata.judgecheck.config import describe_judge, judge_spec_from_settings
from strata.judgecheck.cost import (
    CapReached,
    Estimate,
    Price,
    PriceUnknown,
    estimate,
    item_prompt_chars,
    parse_price,
    resolve_price,
)
from strata.judgecheck.items import Item, SetIntegrityError, load_set
from strata.judgecheck.runner import JudgeSpec, RunOutcome, run
from strata.judgecheck.scoring import (
    SCHEMA,
    IncompleteInfo,
    ItemResult,
    JudgeCheckScore,
    score,
)

__all__ = [
    "BARS_V1",
    "SCHEMA",
    "Bars",
    "CapReached",
    "Estimate",
    "IncompleteInfo",
    "Item",
    "ItemResult",
    "JudgeCheckScore",
    "JudgeSpec",
    "Price",
    "PriceUnknown",
    "RunOutcome",
    "SetIntegrityError",
    "compare",
    "describe_judge",
    "estimate",
    "item_prompt_chars",
    "judge_spec_from_settings",
    "load_set",
    "parse_price",
    "resolve_price",
    "run",
    "score",
]
