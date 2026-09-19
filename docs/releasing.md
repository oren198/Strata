# Releasing Strata

A release goes from a finished version branch to PyPI in this order. Each step
has an owner. The operator's steps are approvals on GitHub; nobody uploads to PyPI by hand.

1. **Version branch → `dev`.** The architect merges `release/vX.Y.Z` into `dev`
   (`--no-ff`) and runs the full suite on the merged tree, with no deselects.
   `pyproject.toml` and `src/strata/__init__.py` already carry `X.Y.Z`.
2. **PR `dev` → `main`.** Factual notes: what shipped, the evidence, the known
   limits. The repository ruleset requires an approving review; the operator
   approves, then the PR is merged.
3. **Tag.** A lightweight tag `vX.Y.Z` on main's merge commit, then push it.
4. **GitHub Release** for the tag, marked Latest, with the same factual notes.
   Link only to public pages.
5. **Publish is triggered by the Release.** Publishing the Release starts
   `.github/workflows/publish.yml` (trusted publishing, no token). Its `publish` job
   runs in the `pypi` environment, which **waits for an operator approval**
   under Actions. An unapproved run never reaches PyPI. That's why 1.11.0 was
   never published: its run sat waiting and was never approved. After creating
   the Release, check that the run exists and tell the operator it's waiting:
   `gh run list --workflow publish.yml --limit 3`.
6. **Verify on PyPI** once the run succeeds: `pipx install strata-mem==X.Y.Z`
   from a clean `HOME`, then `strata --version` prints `X.Y.Z`.
7. **Sync branches.** Fast-forward `dev` (and the version branch, if kept) to `main`.
