"""Tests for the scope summary markdown persistence layer.

Each test uses the ``tmp_path`` fixture so there is no shared on-disk state.

Vocabulary follows ``CONTEXT.md`` verbatim: *scope*, *scope summary*,
*directive*, *context*.
"""

from __future__ import annotations

from pathlib import Path

from strata.summary_store import Directive, ScopeSummary, SummaryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_directive(
    *,
    id: str = "c_abc123",
    content: str = "use gRPC, not REST",
    subject: str | None = "rpc-protocol",
    source_scope_id: str = "g_arch",
    source_skill: str = "architect",
    created_at: str = "2026-05-23T10:00:00Z",
) -> Directive:
    return Directive(
        id=id,
        content=content,
        subject=subject,
        source_scope_id=source_scope_id,
        source_skill=source_skill,
        created_at=created_at,
    )


def _make_summary(
    *,
    scope_id: str = "g_arch",
    directives: list[Directive] | None = None,
    context: str = "Architecture decisions for the global scope.",
    updated_at: str = "2026-05-23T20:15:00Z",
) -> ScopeSummary:
    return ScopeSummary(
        scope_id=scope_id,
        directives=directives if directives is not None else [_make_directive()],
        context=context,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# Test 1 — path_for is deterministic and inside the summaries dir
# ---------------------------------------------------------------------------


def test_path_for_deterministic(tmp_path: Path) -> None:
    """``path_for`` returns a stable path inside the summaries directory."""
    store = SummaryStore(str(tmp_path))
    p1 = store.path_for("g_arch")
    p2 = store.path_for("g_arch")
    assert p1 == p2
    assert p1.parent == tmp_path
    assert p1.name == "g_arch.md"


# ---------------------------------------------------------------------------
# Test 2 — read on a missing scope returns None
# ---------------------------------------------------------------------------


def test_read_missing_scope_returns_none(tmp_path: Path) -> None:
    """Reading a scope with no summary file returns ``None``."""
    store = SummaryStore(str(tmp_path))
    assert store.read("nonexistent_scope") is None


# ---------------------------------------------------------------------------
# Test 3 — round-trip with two directives and non-empty context
# ---------------------------------------------------------------------------


def test_round_trip_two_directives_with_context(tmp_path: Path) -> None:
    """Write a summary with two directives and context; read returns equal object."""
    store = SummaryStore(str(tmp_path))

    d1 = _make_directive(
        id="c_a1b2c3",
        content="use gRPC, not REST",
        subject="rpc-protocol",
        created_at="2026-05-23T10:00:00Z",
    )
    d2 = _make_directive(
        id="c_d4e5f6",
        content="all services emit OpenTelemetry metrics",
        subject="observability",
        created_at="2026-05-23T11:00:00Z",
    )
    original = ScopeSummary(
        scope_id="g_arch",
        directives=[d1, d2],
        context="Architecture decisions for the global scope.",
        updated_at="2026-05-23T20:15:00Z",
    )

    store.write("g_arch", original)
    result = store.read("g_arch")

    assert result is not None
    assert result == original


# ---------------------------------------------------------------------------
# Test 4 — zero directives and empty context round-trips correctly
# ---------------------------------------------------------------------------


def test_round_trip_empty_directives_and_context(tmp_path: Path) -> None:
    """Write a summary with no directives and empty context; read returns equivalent."""
    store = SummaryStore(str(tmp_path))
    original = ScopeSummary(
        scope_id="empty_scope",
        directives=[],
        context="",
        updated_at="2026-05-23T08:00:00Z",
    )
    store.write("empty_scope", original)
    result = store.read("empty_scope")

    assert result is not None
    assert result.directives == []
    assert result.context == ""


# ---------------------------------------------------------------------------
# Test 5 — .tmp files are ignored by list_scopes_with_summaries
# ---------------------------------------------------------------------------


def test_tmp_files_ignored_by_list(tmp_path: Path) -> None:
    """A leftover ``.tmp`` file is never returned by ``list_scopes_with_summaries``."""
    store = SummaryStore(str(tmp_path))

    # Write a real summary
    store.write("real_scope", _make_summary(scope_id="real_scope"))

    # Simulate a crashed write by placing a .tmp file directly
    (tmp_path / "crashed_scope.md.tmp").write_text("partial content", encoding="utf-8")

    scopes = store.list_scopes_with_summaries()
    assert "real_scope" in scopes
    assert "crashed_scope" not in scopes
    # The .tmp filename is not .md so its stem should not appear
    assert not any("tmp" in s for s in scopes)


# ---------------------------------------------------------------------------
# Test 6 — overwrite: second write wins
# ---------------------------------------------------------------------------


def test_overwrite_returns_latest(tmp_path: Path) -> None:
    """Writing a second summary for the same scope replaces the first.

    Version is bumped on each write, so the second write produces version=2.
    """
    store = SummaryStore(str(tmp_path))

    first = ScopeSummary(
        scope_id="g_arch",
        directives=[_make_directive(content="first version")],
        context="initial context",
        updated_at="2026-05-23T10:00:00Z",
    )
    second = ScopeSummary(
        scope_id="g_arch",
        directives=[_make_directive(content="second version")],
        context="updated context",
        updated_at="2026-05-23T11:00:00Z",
    )

    store.write("g_arch", first)
    store.write("g_arch", second)

    result = store.read("g_arch")
    assert result is not None
    # Version is bumped on each write: first write → 1, second write → 2
    assert result.version == 2
    assert result.directives[0].content == "second version"
    assert result.context == "updated context"
    assert result.updated_at == second.updated_at


# ---------------------------------------------------------------------------
# Test 7 — delete removes the file and returns True; second call returns False
# ---------------------------------------------------------------------------


def test_delete_idempotent(tmp_path: Path) -> None:
    """``delete`` returns True when the file existed, False when it did not."""
    store = SummaryStore(str(tmp_path))
    store.write("scope_to_delete", _make_summary(scope_id="scope_to_delete"))

    assert store.exists("scope_to_delete") is True
    assert store.delete("scope_to_delete") is True
    assert store.exists("scope_to_delete") is False
    assert store.delete("scope_to_delete") is False


# ---------------------------------------------------------------------------
# Test 8 — list_scopes_with_summaries ignores .tmp, hidden files, non-.md
# ---------------------------------------------------------------------------


def test_list_scopes_ignores_non_md_files(tmp_path: Path) -> None:
    """Only ``<id>.md`` files are returned; hidden, .tmp, and other files are skipped."""
    store = SummaryStore(str(tmp_path))

    # Real summaries
    store.write("scope_a", _make_summary(scope_id="scope_a"))
    store.write("scope_b", _make_summary(scope_id="scope_b"))

    # Noise that must be ignored
    (tmp_path / ".hidden_scope.md").write_text("hidden", encoding="utf-8")
    (tmp_path / "scope_c.md.tmp").write_text("tmp", encoding="utf-8")
    (tmp_path / "scope_d.json").write_text("{}", encoding="utf-8")
    (tmp_path / "scope_e.txt").write_text("text", encoding="utf-8")

    scopes = store.list_scopes_with_summaries()
    assert sorted(scopes) == ["scope_a", "scope_b"]


def test_list_scopes_ignores_publication_artifacts(tmp_path: Path) -> None:
    """A scope's ``<id>.pub.md`` publication artifact is never listed as a scope summary.

    ADR 0007 D1: the publication artifact is a sibling file in the same
    summaries directory, not a scope summary — list_scopes_with_summaries
    must exclude it even though it ends in ``.md``.
    """
    store = SummaryStore(str(tmp_path))
    store.write("scope_a", _make_summary(scope_id="scope_a"))

    # A publication artifact for a scope that has NO summary of its own —
    # proves this isn't merely "scope_a's .pub.md is masked by scope_a.md".
    (tmp_path / "scope_only_publishes.pub.md").write_text("pub", encoding="utf-8")
    (tmp_path / "scope_a.pub.md").write_text("pub", encoding="utf-8")

    scopes = store.list_scopes_with_summaries()
    assert sorted(scopes) == ["scope_a"]
    assert "scope_only_publishes" not in scopes
    assert "scope_only_publishes.pub" not in scopes


# ---------------------------------------------------------------------------
# Test 9 — markdown output contains the expected section headings
# ---------------------------------------------------------------------------


def test_markdown_format_contains_section_headings(tmp_path: Path) -> None:
    """Written file contains the literal ``## Directives`` and ``## Context`` headings."""
    store = SummaryStore(str(tmp_path))
    store.write("g_arch", _make_summary(scope_id="g_arch"))

    raw = store.path_for("g_arch").read_text(encoding="utf-8")

    assert "## Directives" in raw
    assert "## Context" in raw
    assert "# Scope: g_arch" in raw


# ---------------------------------------------------------------------------
# Test 10 — contribution ID [c_xxx] heading format preserved on round-trip
# ---------------------------------------------------------------------------


def test_directive_id_preserved_on_round_trip(tmp_path: Path) -> None:
    """The directive's contribution ID is exactly preserved through write → read."""
    store = SummaryStore(str(tmp_path))
    directive = _make_directive(id="c_abc123")
    summary = ScopeSummary(
        scope_id="id_test_scope",
        directives=[directive],
        context="",
        updated_at="2026-05-23T12:00:00Z",
    )
    store.write("id_test_scope", summary)

    raw = store.path_for("id_test_scope").read_text(encoding="utf-8")
    # The heading must contain the bracketed ID
    assert "[c_abc123]" in raw

    result = store.read("id_test_scope")
    assert result is not None
    assert len(result.directives) == 1
    assert result.directives[0].id == "c_abc123"


# ---------------------------------------------------------------------------
# Test 11 — version=0 / exists=False sentinel for synthesized summaries
# (issue #59): a synthesized empty summary must be distinguishable from a
# real first write.
# ---------------------------------------------------------------------------


def test_synthesized_summary_defaults_are_a_real_write() -> None:
    """Constructing a ScopeSummary with no version/exists override defaults
    to version=1, exists=True — i.e. callers that synthesize a placeholder
    for "no summary yet" must pass version=0, exists=False explicitly;
    the model's own defaults describe a real write, not a placeholder.
    """
    summary = ScopeSummary(
        scope_id="g_new",
        directives=[],
        context="",
        updated_at="2026-05-23T12:00:00Z",
    )
    assert summary.version == 1
    assert summary.exists is True


def test_synthesized_empty_summary_reads_as_version_zero_not_exists() -> None:
    """A synthesized placeholder (as API/MCP callers build for a scope with
    no on-disk summary) explicitly reports version=0, exists=False.
    """
    empty = ScopeSummary(
        scope_id="g_new",
        directives=[],
        context="",
        updated_at="2026-05-23T12:00:00Z",
        version=0,
        exists=False,
    )
    assert empty.version == 0
    assert empty.exists is False


def test_first_real_write_is_version_one_and_exists(tmp_path: Path) -> None:
    """A scope's first real write reports version=1, exists=True — distinct
    from the version=0/exists=False synthesized placeholder for "no summary
    yet" (issue #59), even though nothing was on disk beforehand either.
    """
    store = SummaryStore(str(tmp_path))
    written = store.write("g_new", _make_summary(scope_id="g_new"))

    assert written.version == 1
    assert written.exists is True

    result = store.read("g_new")
    assert result is not None
    assert result.version == 1
    assert result.exists is True


def test_write_forces_exists_true_even_if_caller_passed_false(tmp_path: Path) -> None:
    """write() always persists a real write as exists=True.

    Defensive: even if a caller mistakenly hands write() a summary built
    with exists=False (the synthesized-placeholder marker), the fact that
    it is being written for real must win — a summary on disk is by
    definition not a synthesized placeholder.
    """
    store = SummaryStore(str(tmp_path))
    mislabeled = ScopeSummary(
        scope_id="g_new",
        directives=[],
        context="",
        updated_at="2026-05-23T12:00:00Z",
        version=0,
        exists=False,
    )
    written = store.write("g_new", mislabeled)

    assert written.version == 1
    assert written.exists is True

    result = store.read("g_new")
    assert result is not None
    assert result.exists is True
