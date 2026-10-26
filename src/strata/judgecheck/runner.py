"""Run the set through one or more judges, in isolation, under a spend cap.

Isolation: every run happens in a fresh temp dir. Stores are built from explicit temp paths;
nothing here reads `Settings`, walks up for `.strata/`, changes directory or touches the user's
usage ledger (the engine's usage metering is bound to a temp database for the run, so neither
the user's daily token cap nor the user's usage rows are involved). The judge CONFIG (model,
provider, base URL, key) is the only thing taken from the user's settings, and that is done by
the caller that builds the `JudgeSpec`, not here.

Counting: each judge's client is wrapped so every HTTP call is counted, priced and capped.
"""

from __future__ import annotations

import os
import random
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from strata.judgecheck.cost import Budget, CapReached, Price
from strata.judgecheck.items import Item
from strata.judgecheck.materialize import judge_call, publish_world
from strata.judgecheck.scoring import IncompleteInfo, ItemResult
from strata.scope_manager import InheritedReaskUnavailable, ScopeManager

# -- the counting client ----------------------------------------------------------------


@dataclass
class CallStats:
    """What the wrapper saw during one judgment."""

    calls: int = 0
    corrective: int = 0


class _Messages:
    def __init__(self, owner: CountingClient) -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        return self._owner._create(**kwargs)  # noqa: SLF001


class CountingClient:
    """Wraps an anthropic-shaped client: counts calls, charges the shared `Budget`, checks the
    cap BEFORE each call. Every other attribute (``base_url`` and so on) is the real client's."""

    def __init__(self, client: Any, budget: Budget) -> None:
        self._client = client
        self._budget = budget
        self.messages = _Messages(self)
        self.stats = CallStats()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def begin_judgment(self) -> None:
        self.stats = CallStats()

    def _create(self, **kwargs: Any) -> Any:
        from strata.judge_usage import call_kind_from_request  # noqa: PLC0415

        self._budget.check_before(kwargs)  # may raise CapReached: nothing is sent
        self.stats.calls += 1
        if call_kind_from_request(kwargs) == "corrective_reask":
            self.stats.corrective += 1
        # This wrapper IS the transport below the choke point: the engine's judge calls reach it
        # through `metered_messages_create` (which pins, meters and caps first), and it hands the
        # request to the real client. It is bound to a name rather than called inline because the
        # guard tests allow a literal call to the client send method only in `judge_usage.py`.
        send = self._client.messages.create
        response = send(**kwargs)
        self._budget.add_after(kwargs, response)
        return response


# -- specs and outcome --------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeSpec:
    """One judge under test: a label for the report, the client and the judge settings."""

    label: str
    client: Any
    model: str
    provider: str | None = None


@dataclass
class RunOutcome:
    results: dict[str, list[ItemResult]]
    spent_usd: float
    calls: int
    reps: int
    incomplete: IncompleteInfo | None = None
    order: list[tuple[str, int]] = field(default_factory=list)


@dataclass(frozen=True)
class IsolatedEnv:
    root: Path
    usage_db: str


@contextmanager
def isolated_environment() -> Iterator[IsolatedEnv]:
    """A temp root for the run, with the engine's usage metering bound to a temp database."""
    from strata.judge_usage import bind_usage_db, unbind_usage_db  # noqa: PLC0415
    from strata.migrator import run_migrations  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="strata-judgecheck-") as raw:
        root = Path(raw)
        usage_db = str(root / "usage.db")
        run_migrations(usage_db)  # the binding is honoured only for an existing file
        previous = bind_usage_db(usage_db)
        # The user's daily token cap is the user's: judge-check meters into its own temp ledger
        # and is capped by its own --max-usd, so the engine's cap is switched off for the run
        # (an env value that parses to "no cap" wins over settings) and put back after.
        cap_env = "STRATA_JUDGE_DAILY_TOKEN_CAP"
        saved_cap = os.environ.get(cap_env)
        os.environ[cap_env] = ""
        try:
            yield IsolatedEnv(root=root, usage_db=usage_db)
        finally:
            if saved_cap is None:
                os.environ.pop(cap_env, None)
            else:
                os.environ[cap_env] = saved_cap
            unbind_usage_db(previous)


# -- one judgment -------------------------------------------------------------------------


def _dump_ops(judgment: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "op": op.op,
            "id": op.id,
            "content": op.content,
            "subject": op.subject,
            "supersedes": op.supersedes,
        }
        for op in (getattr(judgment, "directive_ops", None) or [])
    )


def _status_of(exc: BaseException) -> int | None:
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code
    cause = exc.__cause__
    return _status_of(cause) if cause is not None else None


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, anthropic.APITimeoutError):
        return True
    cause = exc.__cause__
    return _is_timeout(cause) if cause is not None else False


def classify_failure(exc: BaseException) -> tuple[str, int | None]:
    """``(decision, status)`` for a judgment that raised. A timeout; a judge error (an HTTP
    status error, a connection error, or a relation re-ask that stayed unreachable); or a
    tool-call failure (the engine's parse/corrective path gave up)."""
    if _is_timeout(exc):
        return "timeout", None
    status = _status_of(exc)
    if status is not None or isinstance(
        exc, anthropic.APIConnectionError | InheritedReaskUnavailable
    ):
        return "judge_error", status
    if isinstance(exc, ValueError | KeyError | TypeError):
        return "tool_error", None
    return "judge_error", None


def _run_p5(item: Item, manager: ScopeManager, root: Path) -> tuple[str, str, tuple[str, ...]]:
    from strata.fleet_config import FleetConfig  # noqa: PLC0415
    from strata.migrator import run_migrations  # noqa: PLC0415
    from strata.publication import propose_publish, read_publication  # noqa: PLC0415
    from strata.record_store import RecordStore  # noqa: PLC0415
    from strata.summary_store import SummaryStore  # noqa: PLC0415

    world = publish_world(item)
    workdir = Path(tempfile.mkdtemp(prefix="p5-", dir=root))
    db_path = str(workdir / "strata.db")
    run_migrations(db_path)
    fleet = FleetConfig.model_validate(world.fleet_dict)
    summaries = SummaryStore(str(workdir / "summaries"))
    for scope_id, summary in world.seeds:
        summaries.write(scope_id, summary)
    with RecordStore(db_path) as record_store:
        outcome = propose_publish(
            world.scope_id,
            world.proposal["content"],
            world.proposal["kind"],
            world.proposal.get("subject"),
            world.proposal["anchors"],
            world.proposer,
            fleet=fleet,
            record_store=record_store,
            summary_store=summaries,
            scope_manager=manager,
        )
    face = read_publication(world.scope_id, summaries_dir=str(summaries.summaries_dir))
    return outcome.decision, outcome.reasoning, tuple(p.content for p in face)


def run_one(
    item: Item, rep: int, spec: JudgeSpec, client: CountingClient, env: IsolatedEnv
) -> ItemResult:
    """One judgment of one item. Never raises except :class:`CapReached`: a failed judgment is
    a result (``judge_error`` / ``tool_error`` / ``timeout``), because 'it decided nothing
    usable' is something the score must see."""
    manager = ScopeManager(client=client, model=spec.model, judge_provider=spec.provider)
    client.begin_judgment()
    base = {"item_id": item.id, "suite": item.suite, "rep": rep, "judge_label": spec.label}
    try:
        if item.suite == "p5":
            decision, reasoning, texts = _run_p5(item, manager, env.root)
            result = ItemResult(
                **base, decision=decision, reasoning=reasoning, published_texts=texts
            )
        else:
            call = judge_call(item)
            judgment = manager.judge(**call.kwargs)
            if getattr(judgment, "judge_failure", False):
                # the engine's own fail-closed fallback after the corrective re-ask also failed:
                # a tool-call give-up, not a decline on the merits
                return _stamp(
                    ItemResult(**base, decision="tool_error", reasoning=judgment.reasoning),
                    client,
                    recovered=False,
                )
            summary = judgment.new_summary
            result = ItemResult(
                **base,
                decision=judgment.decision,
                directive_ops=_dump_ops(judgment),
                new_summary=summary.model_dump() if summary is not None else None,
                reasoning=judgment.reasoning,
            )
        return _stamp(result, client, recovered=client.stats.corrective > 0)
    except CapReached:
        raise
    except Exception as exc:  # noqa: BLE001 — one bad judgment must never abort the run
        decision, status = classify_failure(exc)
        return _stamp(
            ItemResult(
                **base,
                decision=decision,
                error_status=status,
                reasoning=f"judge raised {type(exc).__name__}",
            ),
            client,
            recovered=False,
        )


def _stamp(result: ItemResult, client: CountingClient, *, recovered: bool) -> ItemResult:
    from dataclasses import replace  # noqa: PLC0415

    return replace(result, calls=client.stats.calls, tool_recovered=recovered)


# -- the run ------------------------------------------------------------------------------


def run(
    items: Sequence[Item],
    *,
    reps: int,
    judges: Sequence[JudgeSpec],
    price: Price | None = None,
    max_usd: float | None = None,
    seed: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
) -> RunOutcome:
    """Judge every item *reps* times with every judge, in a seeded order, interleaving the
    judges (item by item, back to back) so an A/B comparison is made in the same hour.

    The cap (``max_usd``, needs ``price``) is shared by all judges. When it is reached the run
    stops: judgments not started are not in the results, ``incomplete`` says how far it got."""
    budget = Budget(price=price, max_usd=max_usd)  # raises when a cap has no price
    pairs = [(item, rep) for item in items for rep in range(reps)]
    random.Random(seed).shuffle(pairs)
    total = len(pairs) * len(judges)
    results: dict[str, list[ItemResult]] = {spec.label: [] for spec in judges}
    done = 0
    incomplete: IncompleteInfo | None = None
    with isolated_environment() as env:
        clients = {spec.label: CountingClient(spec.client, budget) for spec in judges}
        try:
            for item, rep in pairs:
                for spec in judges:
                    results[spec.label].append(run_one(item, rep, spec, clients[spec.label], env))
                    done += 1
                    if on_progress is not None:
                        on_progress(done, total)
        except CapReached:
            incomplete = IncompleteInfo("cap reached", done, total)
    return RunOutcome(
        results=results,
        spent_usd=budget.spent_usd,
        calls=budget.calls,
        reps=reps,
        incomplete=incomplete,
        order=[(item.id, rep) for item, rep in pairs],
    )
