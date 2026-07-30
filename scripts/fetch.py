"""Phase 3 — fetch candidate product pages to a local cache.

Cache first, parse never here. The parser gets rewritten many times and each
rewrite has to cost zero requests, so this step only ever writes HTML to disk.

robots.txt allows /products/ and sets no Crawl-delay; the pacing below is our
own restraint, not a stated limit.
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from curl_cffi import requests

HEADERS = {
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

MIN_DELAY, MAX_DELAY = 1.5, 3.0
MAX_ATTEMPTS = 3
BACKOFF_BASE = 4.0

# A page this small is a challenge interstitial or an error, not a product.
MIN_PAGE_BYTES = 20_000


def normalise_id(value: object) -> str:
    """Product ids are opaque strings, never numbers.

    Read as numbers they pick up a float tail ('28516011' -> '28516011.0') and
    the same product ends up with two cache keys and two fetches.
    """
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def targets_from_candidates(path: Path, tiers: set[str], top_n: int) -> list[tuple[str, str]]:
    df = pd.read_csv(path, dtype=str)
    df = df[df.tier.isin(tiers)]
    seen: dict[str, str] = {}
    for _, row in df.iterrows():
        # An auto match only needs its winner; a review needs its rivals too,
        # because the page is what decides between them.
        limit = 1 if row.tier == "auto" else top_n
        for rank in range(1, limit + 1):
            pid, url = row.get(f"cand{rank}_id"), row.get(f"cand{rank}_url")
            if pd.notna(pid) and pd.notna(url) and str(url).startswith("http"):
                seen.setdefault(normalise_id(pid), str(url))
    return sorted(seen.items())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", default=Path("F:/Ocado/data/candidates.csv"), type=Path)
    ap.add_argument("--raw-dir", default=Path("F:/Ocado/raw"), type=Path)
    ap.add_argument("--manifest", default=Path("F:/Ocado/data/fetch_manifest.jsonl"), type=Path)
    ap.add_argument("--tiers", default="auto,review")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="stop after N fetches (calibration run)")
    args = ap.parse_args()

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    targets = targets_from_candidates(args.candidates, set(args.tiers.split(",")), args.top_n)

    pending = [(pid, url) for pid, url in targets if not (args.raw_dir / f"{pid}.html.gz").exists()]
    cached = len(targets) - len(pending)
    if args.limit:
        pending = pending[: args.limit]

    print(f"targets {len(targets):,} | cached {cached:,} | to fetch {len(pending):,}")
    if not pending:
        return 0

    counts = {"ok": 0, "404": 0, "failed": 0}
    with args.manifest.open("a", encoding="utf-8") as manifest, \
            requests.Session(impersonate="chrome", headers=HEADERS, timeout=60) as session:
        for n, (pid, url) in enumerate(pending, 1):
            record = {"product_id": pid, "url": url,
                      "fetched_at": datetime.now(timezone.utc).isoformat()}

            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    r = session.get(url)
                except Exception as exc:  # noqa: BLE001
                    record.update(status="error", detail=f"{type(exc).__name__}: {exc}")
                    time.sleep(BACKOFF_BASE * attempt)
                    continue

                if r.status_code == 404:
                    # The sitemap has no lastmod, so a delisted product looks
                    # exactly like a live one until it 404s. Not a bug.
                    record.update(status="404", http=404)
                    break

                if r.status_code == 200 and len(r.content) >= MIN_PAGE_BYTES:
                    (args.raw_dir / f"{pid}.html.gz").write_bytes(
                        gzip.compress(r.content, compresslevel=6))
                    record.update(status="ok", http=200, bytes=len(r.content))
                    break

                waf = r.headers.get("x-amzn-waf-action")
                record.update(status="blocked" if waf else "http_error",
                              http=r.status_code, bytes=len(r.content),
                              detail=f"waf={waf}" if waf else "")
                if r.status_code in (429, 403) or r.status_code >= 500 or waf:
                    time.sleep(BACKOFF_BASE * attempt + random.uniform(0, 2))
                    continue
                break

            manifest.write(json.dumps(record) + "\n")
            manifest.flush()
            key = "ok" if record["status"] == "ok" else ("404" if record["status"] == "404" else "failed")
            counts[key] += 1

            if n % 25 == 0 or n == len(pending):
                print(f"  {n:>4}/{len(pending)}  ok={counts['ok']} 404={counts['404']} failed={counts['failed']}")
            if record["status"] != "404":
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    print(json.dumps(counts, indent=2))
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
