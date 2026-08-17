from __future__ import annotations

import argparse
from pathlib import Path

from mdrk_builder.infrastructure.discharge_summary_template import (
    create_discharge_summary_template,
    discharge_summary_template_path,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the sanitized discharge-summary runtime template."
    )
    parser.add_argument("reference", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=discharge_summary_template_path(),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = create_discharge_summary_template(args.reference, args.output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
