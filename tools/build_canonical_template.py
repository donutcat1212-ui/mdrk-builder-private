from __future__ import annotations

import argparse
from pathlib import Path

from mdrk_builder.infrastructure.docx_template import (
    canonical_template_path,
    create_canonical_template,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the sanitized canonical MDRK DOCX template")
    parser.add_argument(
        "--output",
        type=Path,
        default=canonical_template_path(),
        help="Destination DOCX path",
    )
    args = parser.parse_args()
    output = create_canonical_template(args.output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
