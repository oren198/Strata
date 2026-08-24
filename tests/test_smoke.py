"""Placeholder smoke test. Real tests land in subsequent features."""


def test_module_imports():
    import re

    import strata

    assert re.fullmatch(r"\d+\.\d+\.\d+", strata.__version__)


def test_version_matches_distribution_metadata() -> None:
    """pyproject's version and strata.__version__ must move together (#117).

    1.6.1 shipped with __version__ still saying 1.6.0 because only pyproject
    was bumped; this pin makes a lone bump fail CI.
    """
    import pathlib
    import re

    import strata

    pyproject = pathlib.Path(strata.__file__).parents[2] / "pyproject.toml"
    declared = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M).group(1)
    assert declared == strata.__version__
