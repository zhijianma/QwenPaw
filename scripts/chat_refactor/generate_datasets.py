# -*- coding: utf-8 -*-
"""Generate deterministic Chat-refactor performance datasets."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qwenpaw.app.chats.acceptance_datasets import (
    DATASET_SPECS,
    write_dataset,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profiles",
        nargs="+",
        choices=[*DATASET_SPECS, "all"],
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Generate selected datasets and print compact result metadata."""
    args = _parse_args()
    names = list(DATASET_SPECS) if "all" in args.profiles else args.profiles
    for name in names:
        payload = write_dataset(
            DATASET_SPECS[name],
            args.output_dir,
            overwrite=args.overwrite,
        )
        result = {
            "name": name,
            "serialized_bytes": payload["serialized_bytes"],
            "generation_seconds": payload["generation_seconds"],
            "transcript_hash": payload["manifest"]["transcript_hash"],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
