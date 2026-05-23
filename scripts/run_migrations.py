"""Placeholder migration runner.

The real implementation — which will apply SQLite schema migrations for the
record store — lands in feature/record-store. This script exists so that
`make migrate` has a valid target from day one.

Vocabulary note: the record store holds the append-only log of every
contribution accepted into each scope. Migrations here evolve that schema.
"""


def main() -> None:
    print("No migrations to run yet. (Placeholder — see feature/record-store.)")


if __name__ == "__main__":
    main()
