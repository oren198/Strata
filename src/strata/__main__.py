"""Entry point for `python -m strata`.

Prints the version and exits. The real CLI (contribute, read perspective,
manage scopes) lands in subsequent features.
"""

import argparse

from strata import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="strata",
        description="Strata — shared memory for agent fleets.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"strata {__version__}",
    )
    # Parse args; --help exits 0 automatically; no subcommands yet.
    parser.parse_args()
    print(f"strata {__version__}")


if __name__ == "__main__":
    main()
