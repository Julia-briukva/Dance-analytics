#!/usr/bin/env python3
"""Copy generated report files to the GitHub Pages site root."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_ROOT / "reports"


def copy_file(source: Path, destination: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(f"Missing generated file: {source}")
    destination.write_bytes(source.read_bytes())


def deploy_report(idd: str) -> list[tuple[Path, Path]]:
    copied: list[tuple[Path, Path]] = []
    for suffix in ("html", "json"):
        source = REPORTS_DIR / f"dancer_{idd}_report.{suffix}"
        destination = PROJECT_ROOT / source.name
        copy_file(source, destination)
        copied.append((source, destination))
    return copied


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy generated reports to the GitHub Pages root.")
    parser.add_argument("--idd", action="append", help="Deploy one dancer report by IDD. Can be passed multiple times.")
    parser.add_argument("--index", action="store_true", help="Also copy reports/index.html to index.html.")
    parser.add_argument("--all", action="store_true", help="Deploy all dancer_*.html/json reports from reports/.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    copied: list[tuple[Path, Path]] = []
    idds = args.idd or []

    if args.all:
        idds = sorted(
            {
                path.name.removeprefix("dancer_").removesuffix("_report.html")
                for path in REPORTS_DIR.glob("dancer_*_report.html")
            }
        )

    for idd in idds:
        copied.extend(deploy_report(idd))

    if args.index:
        source = REPORTS_DIR / "index.html"
        destination = PROJECT_ROOT / "index.html"
        copy_file(source, destination)
        copied.append((source, destination))

    if not copied:
        raise SystemExit("Nothing to deploy. Use --idd, --index, or --all.")

    for source, destination in copied:
        print(f"{source.relative_to(PROJECT_ROOT)} -> {destination.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
