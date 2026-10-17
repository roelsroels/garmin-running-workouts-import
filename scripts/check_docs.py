from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^]]*]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md"]
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    return files


def local_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    else:
        target = target.split(maxsplit=1)[0]

    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    return unquote(parsed.path)


def main() -> int:
    failures: list[str] = []
    checked = 0

    for document in markdown_files():
        for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), start=1):
            for match in MARKDOWN_LINK.finditer(line):
                target = local_target(match.group(1))
                if target is None:
                    continue

                checked += 1
                destination = (document.parent / target).resolve()
                try:
                    destination.relative_to(ROOT)
                except ValueError:
                    failures.append(f"{document.relative_to(ROOT)}:{line_number}: target leaves repository: {target}")
                    continue

                if not destination.exists():
                    failures.append(f"{document.relative_to(ROOT)}:{line_number}: missing target: {target}")

    if failures:
        print("Documentation link check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"Checked {checked} repository-local documentation links and image references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
