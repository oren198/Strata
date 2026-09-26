# Releasing Strata

A release goes from a finished version branch to PyPI in this order. Each step
has an owner. The operator's steps are approvals on GitHub; nobody uploads to PyPI by hand.

1. **Version branch → `dev`.** The architect merges `release/vX.Y.Z` into `dev`
   (`--no-ff`) and runs the full suite on the merged tree, with no deselects.
   `pyproject.toml` and `src/strata/__init__.py` already carry `X.Y.Z`.
2. **Bump the version.** `pyproject.toml` and `src/strata/__init__.py` carry `X.Y.Z` before
   the PR is opened, not after. v1.13.0 reached its release PR still reading 1.12.0, caught
   only because a verification printed `strata.__version__`; the suite does not check it.
3. **PR `dev` → `main`.** Factual notes: what shipped, the evidence, the known
   limits. The repository ruleset requires an approving review; the operator
   approves, then the PR is merged.
4. **Tag.** A lightweight tag `vX.Y.Z` on main's merge commit, then push it.
5. **GitHub Release** for the tag, marked Latest, with the same factual notes.
   Link only to public pages.
6. **Publish is triggered by the Release.** Publishing the Release starts
   `.github/workflows/publish.yml` (trusted publishing, no token). Its `publish` job
   runs in the `pypi` environment, which **waits for an operator approval**
   under Actions. An unapproved run never reaches PyPI. That's why 1.11.0 was
   never published: its run sat waiting and was never approved. After creating
   the Release, check that the run exists and tell the operator it's waiting:
   `gh run list --workflow publish.yml --limit 3`.
7. **Verify on PyPI** once the run succeeds: `pipx install strata-mem==X.Y.Z`
   from a clean `HOME`, then `strata --version` prints `X.Y.Z`.
8. **Sync branches.** Fast-forward `dev` (and the version branch, if kept) to `main`.

9. **Lint the merged tree, not only the suite.** CI runs `ruff check src tests` and
   `ruff format --check src tests` over the whole tree; a branch that is clean on its own can
   still merge into a tree that is not. v1.13.0's release PR failed on 11 such errors.
10. **Evidence before claims.** Any measurement a public claim rests on lives in THIS repo,
   verbatim, before the claim is made — `docs/evidence/`, with local paths and key references
   scrubbed. The eval repository is not public, so a link to it is a 404 for every reader (the
   1.11.0 notes hit exactly that).
11. **Check the default judge model id still resolves before tagging.** The wheel-smoke CI step
   makes no network call, so a router outage or a retired model id would surface only in
   `strata doctor`, on a stranger's first run.
12. **Run the strata-evals mechanical suite against the merged tree.** Add it to every
   merged-tree verification, alongside lint and the Strata suite:
   `STRATA_EVALS_STRATA_REPO=<merged tree> PYTHONPATH=<merged tree>/src:src pytest` in
   strata-evals. It is the only check that calls the judge interface from outside this repo. In
   v1.15, P3 passed a new keyword to every `judge()` call; all in-repo fakes were updated, so the
   Strata suite passed, while every out-of-repo judge broke with a 500 on each `/contribute`.
13. **Test the judge path with live-shape responses, not only fake judges.** For any branch that
   changes what the judge returns or how it is parsed, at least one test replays a real captured
   judge response (the raw `tool_use` input from a live call) through the real parse and contribute
   path. In v1.16 P5, every fake-judge test passed, but live qwen output went down an unguarded
   parse branch. The operator evidence was silently dropped, raised contributions were forced to
   decline, and an out-of-vocabulary decision reached the database and returned a 500 on
   `/contribute`. A contribute path must never 500 on a judge verdict: an unexpected decision fails
   closed through the re-ask and the recorded unreadable-judgment decline.
