"""Judge usage metering (#246).

One row per judge call, recorded from the single choke point
(:func:`metered_messages_create`). The call kind is derived from the request
(tool name, and whether the conversation is a follow-up). Tokens come from
the response's ``usage``. A failure to record is logged and never raised.

The optional daily cap (``STRATA_JUDGE_DAILY_TOKEN_CAP``) is checked only
before a judgment's first call and before standalone calls. A follow-up of
a judgment that has already started is not checked: that judgment finishes.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_OVERFLOW_MARK = "over the BUDGET"

# The cap applies only to these. Every other kind is ungated, including a
# tool name this module does not know: a judgment that has started always
# finishes, and a new re-ask cannot be stranded on the cap by being unmapped.
_GATED_KINDS = frozenset(
    {
        "judgment",
        "batch_judgment",
        "carrier_check",
        "publication",
        "bootstrap",
        "drafter",
        "doctor_probe",
    }
)

_FIRST_KIND = {
    "submit_judgment": "judgment",
    "submit_batch_judgment": "batch_judgment",
    "classify_interior_assertion": "targeted_reask",
    "recheck_attribution": "attribution_recheck",
    "recheck_relation": "relation_recheck",
    "classify_inherited_relation": "inherited_relation_recheck",
    "classify_claim_carriers": "carrier_check",
    "submit_publication_judgment": "publication",
    "submit_bootstrap_publication": "bootstrap",
    "record_freshness_verdict": "drafter",
}

_FOLLOW_KIND = {
    "submit_judgment": "corrective_reask",
    "submit_batch_judgment": "batch_corrective_reask",
}

_SCOPE_MARK = "SCOPE: "
_CONTRIBUTION_MARK = "NEW CONTRIBUTION TO JUDGE:\n- id: "

_bound_db: str | None = None


class JudgeDailyCapReached(RuntimeError):
    """Today's judge tokens are already at the configured cap.

    Raised before the model is called. Callers treat it the same way they
    treat an unreachable judge: the contribution stays unjudged, the attempt
    is recorded, and a later re-judge can send it.
    """

    def __init__(self, used: int, cap: int) -> None:
        super().__init__(
            f"Judge daily token cap reached ({used} of {cap} tokens used today). "
            "This call was not sent."
        )
        self.used = used
        self.cap = cap


def parse_daily_token_cap(value: object) -> int | None:
    """``None`` when the cap is unset, blank, or not a non-negative integer.

    A value that is not a usable cap turns the cap off. It is never treated
    as zero, which would refuse every call.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip().strip('"').strip("'")
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def configured_daily_token_cap() -> int | None:
    """The process cap. An explicit environment value wins over the cached settings."""
    if "STRATA_JUDGE_DAILY_TOKEN_CAP" in os.environ:
        return parse_daily_token_cap(os.environ.get("STRATA_JUDGE_DAILY_TOKEN_CAP"))
    try:
        from strata.settings import get_settings  # noqa: PLC0415

        return get_settings().judge_daily_token_cap
    except Exception:  # noqa: BLE001 — a settings failure must not block a judgment
        _logger.warning("judge daily cap could not be read", exc_info=True)
        return None


def bind_usage_db(path: str | None) -> str | None:
    """Record usage to *path* for this process. Returns the previous binding."""
    global _bound_db
    previous = _bound_db
    _bound_db = path
    return previous


def unbind_usage_db(previous: str | None) -> None:
    global _bound_db
    _bound_db = previous


def current_usage_db() -> str | None:
    """The database usage rows are written to, or ``None`` when there is no store yet."""
    if _bound_db and Path(_bound_db).is_file():
        return _bound_db
    try:
        from strata.project_config import resolve_storage_paths  # noqa: PLC0415
        from strata.settings import get_settings  # noqa: PLC0415

        path = resolve_storage_paths(get_settings()).db_path
    except Exception:  # noqa: BLE001 — resolving storage must not block a judgment
        return None
    if path and Path(path).is_file():
        return path
    return None


def kind_is_gated(kind: str) -> bool:
    """True when this call is checked against the daily cap.

    Only a judgment's first call and the known standalone calls are gated.
    A follow-up, a targeted re-ask, and any tool name that is not mapped are
    not: a judgment that has started always finishes.
    """
    return kind in _GATED_KINDS


def call_kind_from_request(kwargs: dict[str, Any]) -> str:
    """Name the call from its tool and whether it continues a judgment already started."""
    tool = _tool_name(kwargs)
    messages = _messages(kwargs)
    if tool is None:
        return "doctor_probe"
    if _is_follow_up(messages) and tool in _FOLLOW_KIND:
        if _is_overflow(messages):
            return "condensation" if tool == "submit_judgment" else "batch_condensation"
        return _FOLLOW_KIND[tool]
    return _FIRST_KIND.get(tool, tool)


def metered_messages_create(client: Any, kwargs: dict[str, Any], *, provider: str | None) -> Any:
    """Pin, enforce the cap, call the model, and record usage.

    *kwargs* is the request the caller already built. This function does not
    add a field to it. The provider pin is the only mutation, and only when
    a pin applies — the same as :func:`strata.settings.apply_provider_pin`.
    """
    from strata.settings import apply_provider_pin  # noqa: PLC0415

    apply_provider_pin(kwargs, provider=provider, client=client)
    kind = call_kind_from_request(kwargs)
    if kind_is_gated(kind):
        _enforce_daily_cap()
    started = time.perf_counter()
    try:
        response = client.messages.create(**kwargs)
    except Exception:
        _safe_record(kwargs, kind, response=None, latency_ms=_elapsed_ms(started))
        raise
    _safe_record(kwargs, kind, response=response, latency_ms=_elapsed_ms(started))
    return response


def judge_usage_report(
    db_path: str | None,
    *,
    since: str | None = None,
    scope_id: str | None = None,
    cap: int | None = None,
) -> dict[str, Any]:
    """Calls, tokens, and cost grouped by day, scope, and call kind.

    *cap* defaults to the configured cap. Cost is filled only from the price
    table. A missing price leaves the cost unset.
    """
    if cap is None:
        cap = configured_daily_token_cap()
    prices, price_note = load_price_table()
    rows = _load_rows(db_path, since=since, scope_id=scope_id)
    grouped = _group_rows(rows, prices)
    today = tokens_today(db_path)
    total = today["input_tokens"] + today["output_tokens"]
    return {
        "cap": cap,
        "over_cap": cap is not None and total >= cap,
        "today": today,
        "price_note": price_note,
        "rows": grouped,
    }


def tokens_today(db_path: str | None) -> dict[str, int]:
    """Input and output tokens recorded since UTC midnight. Missing store is zero."""
    empty = {"input_tokens": 0, "output_tokens": 0}
    if not db_path or not Path(db_path).is_file():
        return empty
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
                FROM judge_usage
                WHERE created_at >= ?
                """,
                (day,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        _logger.warning("judge usage for today could not be read", exc_info=True)
        return empty
    if row is None:
        return empty
    return {"input_tokens": int(row[0]), "output_tokens": int(row[1])}


def cap_status_text(db_path: str | None, *, cap: int | None = None) -> str:
    """One sentence for ``strata doctor`` and the stats header."""
    if cap is None:
        cap = configured_daily_token_cap()
    today = tokens_today(db_path)
    total = today["input_tokens"] + today["output_tokens"]
    usage = f"today {total} tokens ({today['input_tokens']} input, {today['output_tokens']} output)"
    if cap is None:
        return f"daily token cap off; {usage}"
    if total >= cap:
        return (
            f"daily token cap {cap} reached ({usage}); "
            "new judgments are refused until the cap resets"
        )
    return f"daily token cap {cap}; {usage}"


def load_price_table() -> tuple[dict[str, tuple[float, float]] | None, str]:
    """Prices per model, per million tokens. ``None`` when no table is configured."""
    path = os.environ.get("STRATA_JUDGE_PRICE_TABLE")
    if not path or not path.strip():
        return None, "No price table is set (STRATA_JUDGE_PRICE_TABLE); tokens only."
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, f"Price table {path} could not be read; tokens only."
    if not isinstance(data, dict):
        return None, f"Price table {path} is not an object; tokens only."
    prices: dict[str, tuple[float, float]] = {}
    for model, entry in data.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            continue
        incoming = _price_number(entry.get("input_per_million"))
        outgoing = _price_number(entry.get("output_per_million"))
        if incoming is None or outgoing is None:
            continue
        prices[model] = (incoming, outgoing)
    return prices, f"Price table: {path}"


def format_judge_usage_report(report: dict[str, Any]) -> str:
    """The text form of :func:`judge_usage_report`."""
    lines = [
        "Judge usage",
        report["price_note"],
        _header_cap(report),
        "",
    ]
    rows = report["rows"]
    if not rows:
        lines.append("No judge calls in this window.")
        return "\n".join(lines)
    header = (
        f"  {'day':<10}  {'scope':<16}  {'call kind':<24}  "
        f"{'calls':>5}  {'input':>8}  {'output':>8}  cost"
    )
    lines.append(header)
    for row in rows:
        scope = row["scope_id"] or "(no scope)"
        cost = _format_cost(row)
        lines.append(
            f"  {row['day']:<10}  {scope:<16}  {row['call_kind']:<24}  "
            f"{row['calls']:>5}  {row['input_tokens']:>8}  {row['output_tokens']:>8}  {cost}"
        )
    return "\n".join(lines)


def _header_cap(report: dict[str, Any]) -> str:
    today = report["today"]
    total = today["input_tokens"] + today["output_tokens"]
    usage = (
        f"Today: {total} tokens ({today['input_tokens']} input, {today['output_tokens']} output)."
    )
    cap = report["cap"]
    if cap is None:
        return f"Daily token cap: off. {usage}"
    if report["over_cap"]:
        return f"Daily token cap: {cap}, reached. {usage} New judgments are refused."
    return f"Daily token cap: {cap}. {usage}"


def _format_cost(row: dict[str, Any]) -> str:
    if row["unpriced_calls"]:
        return "no price set"
    cost = row["cost"]
    if cost is None:
        return "no price set"
    return f"{cost:.6f}"


def _group_rows(
    rows: list[sqlite3.Row], prices: dict[str, tuple[float, float]] | None
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for row in rows:
        day = str(row["created_at"])[:10]
        key = (day, row["scope_id"], row["call_kind"])
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "day": day,
                "scope_id": row["scope_id"],
                "call_kind": row["call_kind"],
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.0,
                "unpriced_calls": 0,
            }
            grouped[key] = bucket
        bucket["calls"] += 1
        bucket["input_tokens"] += row["input_tokens"] or 0
        bucket["output_tokens"] += row["output_tokens"] or 0
        price = None if prices is None or row["model"] is None else prices.get(row["model"])
        if price is None or row["input_tokens"] is None or row["output_tokens"] is None:
            bucket["unpriced_calls"] += 1
        else:
            incoming, outgoing = price
            bucket["cost"] += (row["input_tokens"] / 1_000_000) * incoming
            bucket["cost"] += (row["output_tokens"] / 1_000_000) * outgoing
    result = []
    for bucket in grouped.values():
        if bucket["unpriced_calls"]:
            bucket["cost"] = None
        result.append(bucket)
    result.sort(key=lambda item: (item["day"], item["scope_id"] or "", item["call_kind"]))
    return result


def _load_rows(
    db_path: str | None, *, since: str | None, scope_id: str | None
) -> list[sqlite3.Row]:
    if not db_path or not Path(db_path).is_file():
        return []
    clauses = ["1 = 1"]
    params: list[str] = []
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if scope_id:
        clauses.append("scope_id = ?")
        params.append(scope_id)
    sql = (
        "SELECT created_at, scope_id, call_kind, model, input_tokens, output_tokens "
        "FROM judge_usage WHERE " + " AND ".join(clauses) + " ORDER BY created_at"
    )
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(sql, params).fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        _logger.warning("judge usage could not be read", exc_info=True)
        return []


def _enforce_daily_cap() -> None:
    cap = configured_daily_token_cap()
    if cap is None:
        return
    db_path = current_usage_db()
    today = tokens_today(db_path)
    used = today["input_tokens"] + today["output_tokens"]
    if used >= cap:
        raise JudgeDailyCapReached(used, cap)


def _safe_record(
    kwargs: dict[str, Any],
    kind: str,
    *,
    response: Any,
    latency_ms: int,
) -> None:
    db_path = current_usage_db()
    if db_path is None:
        return
    try:
        _insert(db_path, kwargs, kind, response=response, latency_ms=latency_ms)
    except Exception:  # noqa: BLE001 — recording never fails a judgment
        _logger.warning("judge usage was not recorded", exc_info=True)


def _insert(
    db_path: str,
    kwargs: dict[str, Any],
    kind: str,
    *,
    response: Any,
    latency_ms: int,
) -> None:
    scope_id, contribution_id = _scope_and_contribution(kwargs)
    usage = getattr(response, "usage", None) if response is not None else None
    model = _response_model(response) or _request_model(kwargs)
    provider = _response_provider(response)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            INSERT INTO judge_usage (
                id, created_at, scope_id, call_kind, contribution_id,
                model, provider, latency_ms, input_tokens, output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"jus_{secrets.token_hex(8)}",
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                scope_id,
                kind,
                contribution_id,
                model,
                provider,
                latency_ms,
                _token_count(usage, "input_tokens"),
                _token_count(usage, "output_tokens"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _token_count(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _response_model(response: Any) -> str | None:
    if response is None:
        return None
    model = getattr(response, "model", None)
    return model if isinstance(model, str) and model else None


def _request_model(kwargs: dict[str, Any]) -> str | None:
    model = kwargs.get("model")
    return model if isinstance(model, str) and model else None


def _response_provider(response: Any) -> str | None:
    """The provider only when the endpoint itself returned one."""
    if response is None:
        return None
    provider = getattr(response, "provider", None)
    return provider if isinstance(provider, str) and provider else None


def _scope_and_contribution(kwargs: dict[str, Any]) -> tuple[str | None, str | None]:
    text = ""
    for message in _messages(kwargs):
        if isinstance(message, dict) and message.get("role") == "user":
            text = _content_text(message.get("content"))
            break
    scope_id = None
    marker = text.find(_SCOPE_MARK)
    if marker >= 0:
        rest = text[marker + len(_SCOPE_MARK) :]
        ident = rest.find("(id=")
        if ident >= 0:
            end = rest.find(")", ident)
            if end > ident + 4:
                scope_id = rest[ident + 4 : end]
    contribution_id = None
    contrib = text.find(_CONTRIBUTION_MARK)
    if contrib >= 0:
        rest = text[contrib + len(_CONTRIBUTION_MARK) :]
        line = rest.splitlines()[0].strip() if rest else ""
        contribution_id = line or None
    return scope_id, contribution_id


def _tool_name(kwargs: dict[str, Any]) -> str | None:
    choice = kwargs.get("tool_choice")
    if isinstance(choice, dict):
        name = choice.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _messages(kwargs: dict[str, Any]) -> list[Any]:
    messages = kwargs.get("messages")
    return messages if isinstance(messages, list) else []


def _is_follow_up(messages: list[Any]) -> bool:
    if len(messages) > 1:
        return True
    return any(
        isinstance(message, dict) and message.get("role") == "assistant" for message in messages
    )


def _is_overflow(messages: list[Any]) -> bool:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _OVERFLOW_MARK in _content_text(message.get("content"))
    return False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _price_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return float(value)


def _elapsed_ms(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))
