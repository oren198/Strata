"""v1.19: a transport error on a relation re-ask fails closed.

A 429, a 5xx, a timeout or a connection error on the #242 / #243 / #244 re-ask is retried with
bounded backoff. If it still fails, nothing is decided on the call that never happened: the
contribution is not admitted on the marker check alone, the publication is not published, and
the failure is recorded as a judge failure so the item waits, unjudged and retryable. An
HTTP-200 answer that is unreadable keeps the documented fail-open path."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

import strata.scope_manager as sm
from strata.scope_manager import InheritedReaskUnavailable, ScopeManager
from tests import test_v18_243_publication_exception as t243
from tests import test_v18_244_matching as t244
from tests import test_v19_own_directive_242 as t242
from tests.test_scope_manager import _fake_response

# the fixture under test is the 243 module's own world
world = t243.world


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sm, "_REASK_BACKOFF_SECONDS", (0.0, 0.0))


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://judge.example/v1/messages")
    response = httpx.Response(status, request=request)
    cls = {
        400: anthropic.BadRequestError,
        429: anthropic.RateLimitError,
        500: anthropic.InternalServerError,
        503: anthropic.InternalServerError,
    }[status]
    return cls(f"status {status}", response=response, body=None)


def _timeout() -> anthropic.APITimeoutError:
    return anthropic.APITimeoutError(request=httpx.Request("POST", "https://judge.example"))


def _connection() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://judge.example"))


ATTEMPTS = len(sm._REASK_BACKOFF_SECONDS) + 1


# -- #242 contribution path -------------------------------------------------


def _context_flow(steps):  # noqa: ANN001, ANN202
    client = MagicMock()
    client.messages.create.side_effect = [_fake_response(t242.FIRST_CONTEXT), *steps]
    contribution = replace(
        t242.NEW_CONTRIBUTION, content=t242.DEPARTURE, proposed_classification="context"
    )
    manager = ScopeManager(client=client)
    kwargs = {
        "scope": t242.SCOPE,
        "stratum": t242.STRATUM,
        "current_summary": t242.SUMMARY,
        "recent_contributions": [],
        "new_contribution": contribution,
    }
    return manager, client, kwargs


def _departure_answer() -> MagicMock:
    return _fake_response(
        t242._answer("departure_report", occurrence_span=t242.DEPARTURE[:-1])  # noqa: SLF001
    )


@pytest.mark.parametrize(
    "error", [_status_error(429), _status_error(503), _timeout(), _connection()]
)
def test_a_transport_error_on_the_context_reask_is_retried_and_then_labelled_as_normal(
    error,
) -> None:  # noqa: ANN001
    manager, client, kwargs = _context_flow([error, _departure_answer()])
    judgment = manager.judge(**kwargs)
    assert client.messages.create.call_count == 3
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["verdict"] == "departure_report"
    assert "re-ask failed" not in judgment.inherited_relation["reason"]
    assert judgment.new_context.endswith(
        f"Departure from c_review01 (this scope): {t242.DEPARTURE}"
    )


@pytest.mark.parametrize(
    "error", [_status_error(429), _status_error(500), _timeout(), _connection()]
)
def test_a_persistent_transport_error_on_the_context_reask_admits_nothing(error) -> None:  # noqa: ANN001
    manager, client, kwargs = _context_flow([error] * ATTEMPTS)
    with pytest.raises(InheritedReaskUnavailable) as raised:
        manager.judge(**kwargs)
    assert client.messages.create.call_count == 1 + ATTEMPTS
    assert type(error).__name__ in str(raised.value)
    assert "relation re-ask failed" in str(raised.value)
    assert isinstance(raised.value.__cause__, type(error))


@pytest.mark.parametrize("error", [_status_error(400), ValueError("boom")])
def test_any_other_failure_keeps_the_unreadable_answer_path(error) -> None:  # noqa: ANN001
    manager, client, kwargs = _context_flow([error])
    judgment = manager.judge(**kwargs)
    assert client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["fallback"] is True
    assert judgment.inherited_relation["reason"].startswith("re-ask failed (")


def test_an_unreadable_http_200_answer_keeps_the_documented_fallback() -> None:
    manager, client, kwargs = _context_flow([_fake_response(t242._answer("banana"))])  # noqa: SLF001
    judgment = manager.judge(**kwargs)
    assert client.messages.create.call_count == 2
    assert judgment.decision == "accept_as_context"
    assert judgment.inherited_relation["fallback"] is True


# -- #243 publication path --------------------------------------------------


def _publication_attempts(world):  # noqa: ANN001, ANN202
    return world[1].list_publication_judgment_attempts(scope_id="g_web")


def test_a_429_on_the_publication_reask_is_retried_and_the_departure_is_labelled(world) -> None:  # noqa: ANN001
    reask = t243._reask(  # noqa: SLF001
        kind="departure_report",
        occurrence_span="On 12 May 2.3.1 was published without the full test suite",
    )
    client = t243._client(_status_error(429), reask)  # noqa: SLF001
    outcome, _ = t243._publish(world, t243._SINGULAR_DEPARTURE, "context", client)  # noqa: SLF001
    assert client.messages.create.call_count == 2
    assert outcome.decision == "accept"
    (item,) = t243._face(world)  # noqa: SLF001
    assert item.content == f"Departure from c_suite (g_org): {t243._SINGULAR_DEPARTURE}"  # noqa: SLF001
    assert _publication_attempts(world) == []


def test_a_persistent_429_on_the_publication_reask_publishes_nothing_and_is_recorded(world) -> None:  # noqa: ANN001
    client = t243._client(*[_status_error(429)] * ATTEMPTS)  # noqa: SLF001
    with pytest.raises(InheritedReaskUnavailable):
        t243._publish(world, t243._SINGULAR_DEPARTURE, "context", client)  # noqa: SLF001
    assert client.messages.create.call_count == ATTEMPTS
    assert t243._face(world) == []  # noqa: SLF001
    (attempt,) = _publication_attempts(world)
    assert attempt.error_class == "InheritedReaskUnavailable"
    assert "RateLimitError" in attempt.message
    assert attempt.outcome == "judge_failed"


def test_a_non_transport_failure_on_the_publication_reask_keeps_the_fallback(world) -> None:  # noqa: ANN001
    client = t243._client(ValueError("boom"))  # noqa: SLF001
    outcome, _ = t243._publish(world, t243._CONSEQUENCE, "context", client)  # noqa: SLF001
    assert outcome.decision == "accept"
    assert outcome.exception_check["fallback"] is True


# -- #244 directive path ----------------------------------------------------


def test_a_429_on_the_directive_reask_is_retried_and_the_directive_is_admitted() -> None:
    legit = "The platform team retains application logs for at most 7 days."
    answer = _fake_response(
        {"relation": "unrelated", "parent_id": "c_suite", "reasoning": "other subject"}
    )
    steps = [_status_error(429), answer]
    judgment, client = _directive_flow(legit, steps)
    assert t244._names(client) == ["submit_judgment"] + ["classify_directive_relation"] * 2  # noqa: SLF001
    assert judgment.decision == "accept_as_directive"
    assert judgment.directive_relation["verdict"] == "admit"
    assert "re-ask failed" not in judgment.directive_relation["reason"]


def test_a_persistent_429_on_the_directive_reask_admits_nothing() -> None:
    with pytest.raises(InheritedReaskUnavailable) as raised:
        _directive_flow(t244._CONTRIBUTION.content, [_status_error(429)] * ATTEMPTS)  # noqa: SLF001
    assert "RateLimitError" in str(raised.value)


def test_a_non_transport_failure_on_the_directive_reask_keeps_the_marker_path() -> None:
    judgment, _ = _directive_flow(t244._CONTRIBUTION.content, [ValueError("boom")])  # noqa: SLF001
    assert judgment.directive_relation["reason"].startswith("re-ask failed (")


def _directive_flow(content, steps):  # noqa: ANN001, ANN202
    client = MagicMock()
    client.messages.create.side_effect = [_fake_response(t244._ADMIT), *steps]  # noqa: SLF001
    contribution = replace(t244._CONTRIBUTION, content=content)  # noqa: SLF001
    judgment = ScopeManager(client=client).judge(
        scope=t244._SCOPE,  # noqa: SLF001
        stratum=t244._STRATUM,  # noqa: SLF001
        current_summary=t244._SUMMARY,  # noqa: SLF001
        recent_contributions=[],
        new_contribution=contribution,
        ancestor_directives=t244._ANCESTORS,  # noqa: SLF001
    )
    return judgment, client


def test_the_transport_check_is_exactly_429_5xx_timeout_and_connection() -> None:
    assert sm._is_transport_error(_status_error(429))  # noqa: SLF001
    assert sm._is_transport_error(_status_error(500))  # noqa: SLF001
    assert sm._is_transport_error(_timeout())  # noqa: SLF001
    assert sm._is_transport_error(_connection())  # noqa: SLF001
    assert not sm._is_transport_error(_status_error(400))  # noqa: SLF001
    assert not sm._is_transport_error(ValueError("x"))  # noqa: SLF001
