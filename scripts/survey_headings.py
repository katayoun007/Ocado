"""Enumerate every heading in the cached pages before writing selectors.

Plan step 2: the risk is schema variety, not volume. Guessing heading text and
discovering later that a field was silently empty on 300 products is the exact
failure the parser is meant to avoid.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

NOISE = ("header", "nav", "footer", "script", "style", "noscript")


def headings(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(list(NOISE)):
        tag.decompose()
    for tag in soup.select('[aria-hidden="true"]'):
        tag.decompose()
    out = []
    for level in range(1, 5):
        for h in soup.find_all(f"h{level}"):
            text = " ".join(h.get_text(" ", strip=True).split())
            if text:
                out.append((f"h{level}", text))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-dir", default=Path("F:/Ocado/raw"), type=Path)
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    files = sorted(args.raw_dir.glob("*.html.gz"))[: args.limit]
    if not files:
        print("no cached pages yet", file=sys.stderr)
        return 1

    counts: Counter[tuple[str, str]] = Counter()
    for path in files:
        html = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
        counts.update(set(headings(html)))

    print(f"{len(files)} pages\n")
    for (level, text), n in counts.most_common(60):
        print(f"  {n:>3}/{len(files)}  {level}  {text[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
