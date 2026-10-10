"""CLI: regenerate the synthetic dataset (``make seed``)."""

import argparse
from pathlib import Path

from _shared.generators import generate_dataset
from _shared.store import default_data_dir, save_dataset


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate seeded synthetic reference data")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scenarios", type=int, default=300)
    parser.add_argument("--out", type=Path, default=default_data_dir())
    args = parser.parse_args(argv)
    path = save_dataset(generate_dataset(args.seed, args.scenarios), args.out)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
