"""An anthropic-shaped client that answers judge-check items from their goldens.

It answers whichever forced tool the engine calls: the judgment tool, the inherited-relation
and directive-relation re-asks, and the publication judge. A set of knobs lets a test flip a
decision, send a malformed tool call (once, or every time), or raise an HTTP error, so the
run's operational counts and verdict lines can be exercised without a network."""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx

from strata.judgecheck import Item


def status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://mock.invalid/v1/messages")
    response = httpx.Response(status, request=request)
    cls = anthropic.RateLimitError if status == 429 else anthropic.InternalServerError
    return cls(f"status {status}", response=response, body=None)


class GoldenClient:
    def __init__(
        self,
        items: Iterable[Item],
        *,
        flip: Iterable[str] = (),
        malformed_once: Iterable[str] = (),
        malformed_always: Iterable[str] = (),
        raise_status: dict[str, int] | None = None,
        usage: tuple[int, int] = (1000, 100),
        base_url: str = "https://mock.invalid/v1",
        api_key: str = "sk-mock-not-a-secret",
    ) -> None:
        self.items = list(items)
        self.flip = set(flip)
        self.malformed_once = set(malformed_once)
        self.malformed_always = set(malformed_always)
        self.raise_status = dict(raise_status or {})
        self.usage = usage
        self.base_url = base_url
        self.api_key = api_key
        self.messages = self
        self.requests: list[dict[str, Any]] = []
        self._seen_malformed: set[str] = set()

    # -- finding the item a request is about ------------------------------------------
    def _item_for(self, text: str) -> Item:
        best: Item | None = None
        best_len = -1
        for item in self.items:
            key = (
                item.data["proposal"]["content"]
                if item.suite == "p5"
                else item.data["contribution"]["content"]
            )
            if key in text and len(key) > best_len:
                best, best_len = item, len(key)
        if best is None:
            raise AssertionError("the mock could not tell which item this request is about")
        return best

    @staticmethod
    def _first_listed_id(text: str) -> str:
        match = re.search(r"^- (\S+?): ", text, re.MULTILINE)
        return match.group(1) if match else "unknown"

    # -- the call --------------------------------------------------------------------
    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        tool = kwargs["tool_choice"]["name"]
        user = kwargs["messages"][0]["content"]
        item = self._item_for(user)
        followup = len(kwargs["messages"]) > 1
        if item.id in self.raise_status:
            raise status_error(self.raise_status[item.id])
        if tool in ("submit_judgment", "submit_publication_judgment"):
            first_time = item.id in self.malformed_once and item.id not in self._seen_malformed
            if item.id in self.malformed_always or first_time:
                self._seen_malformed.add(item.id)
                return self._response(None, tool)  # no tool_use block
        del followup
        return self._response(self._answer(tool, item, user), tool)

    def _response(self, payload: dict[str, Any] | None, tool: str) -> Any:
        block = SimpleNamespace(type="tool_use", name=tool, id="tu_mock", input=payload)
        content = [block] if payload is not None else [SimpleNamespace(type="text", text="oops")]
        return SimpleNamespace(
            content=content,
            stop_reason="tool_use" if payload is not None else "end_turn",
            usage=SimpleNamespace(input_tokens=self.usage[0], output_tokens=self.usage[1]),
        )

    # -- the answers -----------------------------------------------------------------
    def _answer(self, tool: str, item: Item, user: str) -> dict[str, Any]:
        golden = item.golden
        if tool == "submit_judgment":
            return self._judgment(item)
        if tool == "submit_publication_judgment":
            accept = golden["decision"] == "accept"
            if item.id in self.flip:
                accept = not accept
            return {
                "decision": "accept" if accept else "decline",
                "reasoning": "mock follows the golden",
            }
        if tool in (
            "classify_inherited_relation",
            "classify_own_directive_relation",
        ):
            if item.suite == "p5" and golden.get("label"):
                content = item.data["proposal"]["content"]
                return {
                    "kind": "departure_report",
                    "inherited_id": golden["departs_from"],
                    "occurrence_span": content[:-1] if content.endswith(".") else content,
                    "reasoning": "a dated departure",
                }
            return {
                "kind": "unrelated",
                "inherited_id": self._first_listed_id(user),
                "reasoning": "r",
            }
        if tool == "classify_directive_relation":
            return {
                "relation": "unrelated",
                "parent_id": self._first_listed_id(user),
                "reasoning": "r",
            }
        raise AssertionError(f"the mock has no answer for tool {tool!r}")

    def _judgment(self, item: Item) -> dict[str, Any]:
        golden = item.golden
        decision = golden["decision"]
        if item.id in self.flip:
            decision = "accept_as_directive" if decision == "decline" else "decline"
        ops: list[dict[str, Any]] = []
        new_context: str | None = None
        if decision == "accept_as_directive":
            admit = golden.get("expected_admit_op") or "append"
            if admit == "publish":
                ops.append({"op": "publish", "content": item.data["contribution"]["content"]})
            else:
                ops.append({"op": "append"})
        for target in golden.get("expected_supersedes", []):
            ops.append({"op": "supersede", "id": target})
        for target in golden.get("expected_retires", []):
            ops.append({"op": "retire", "id": target})
        if decision != "decline":
            marker = item.data.get("post_check_params", {}).get("marker", "")
            new_context = (
                f"Context note. {marker}".strip() if decision == "accept_as_context" else None
            )
        return {
            "decision": decision,
            "reasoning": "The mock follows the golden answer.",
            "directive_ops": ops,
            "new_context": new_context,
        }
