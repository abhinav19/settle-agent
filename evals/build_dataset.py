"""Regenerate evals/dataset/v<N>/ from the seeded generator:  python build_dataset.py"""

from _shared.store import load_dataset as load_reference

from evalharness.dataset import DEFAULT_DIR, write_dataset

if __name__ == "__main__":
    manifest = write_dataset(load_reference())
    print(f"wrote {manifest.n_cases} cases to {DEFAULT_DIR} (mix {manifest.mix})")
