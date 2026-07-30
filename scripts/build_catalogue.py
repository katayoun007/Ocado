"""Phase 1 — download the Ocado sitemap and turn it into a local index.

Two requests total. Everything after this is offline, which is the point: the
matcher gets rewritten many times and none of those rewrites cost a request.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from curl_cffi import requests
from lxml import etree

sys.path.insert(0, str(Path(__file__).parent))
from canon import extract_quantities  # noqa: E402

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
      "image": "http://www.google.com/schemas/sitemap-image/1.1"}

BASE = "https://www.ocado.com/sitemaps"
INDEX_URL = f"{BASE}/sitemap_index.xml"

HEADERS = {
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# The sitemap standard caps a file at 50,000 URLs. Landing near that number means
# the catalogue is probably truncated and a part2 exists whether or not the index
# admits it (section 5.3) — sitemap-collection-images-part1.xml is precedent.
SITEMAP_URL_CAP = 50_000
TRUNCATION_WARN_AT = 49_000


def fetch(session, url: str) -> bytes | None:
    r = session.get(url)
    if r.status_code == 200 and r.content[:200].lstrip().startswith(b"<"):
        return r.content
    waf = r.headers.get("x-amzn-waf-action")
    detail = f" (x-amzn-waf-action={waf})" if waf else ""
    print(f"  {r.status_code} {len(r.content):,}B{detail}  {url}", file=sys.stderr)
    return None


def parse_products(xml: bytes) -> list[dict]:
    root = etree.fromstring(xml)
    rows = []
    for url_el in root.findall("sm:url", NS):
        loc = url_el.findtext("sm:loc", namespaces=NS)
        if not loc:
            continue
        parts = loc.rstrip("/").split("/")
        product_id = parts[-1]
        slug = parts[-2] if len(parts) >= 2 else ""
        image = url_el.findtext("image:image/image:loc", namespaces=NS) or ""

        quantities = extract_quantities(slug.replace("-", " "))
        rows.append({
            "product_id": product_id,
            # Every id observed ends in 011; keep the base but never assume it.
            "base_id": product_id[:-3] if product_id.endswith("011") else "",
            "slug": slug,
            "url": loc,
            "image_url": image,
            "tokens": slug.split("-") if slug else [],
            "weight_value": quantities["weight"][1] if quantities["weight"] else None,
            "weight_unit": quantities["weight"][0] if quantities["weight"] else None,
            "pack_count": quantities["count"],
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=Path("F:/Ocado/data"), type=Path)
    ap.add_argument("--probe-part2", action="store_true",
                    help="look for part2..part5 even though the index omits them")
    args = ap.parse_args()

    stamp = date.today().isoformat()
    snap_dir = args.out_dir / "sitemaps"
    snap_dir.mkdir(parents=True, exist_ok=True)

    with requests.Session(impersonate="chrome", headers=HEADERS, timeout=60) as s:
        index = fetch(s, INDEX_URL)
        if index is None:
            print("\nBlocked before we started. Nothing was written.", file=sys.stderr)
            return 2
        (snap_dir / f"sitemap_index-{stamp}.xml").write_bytes(index)

        declared = [el.text for el in etree.fromstring(index).findall("sm:sitemap/sm:loc", NS)]
        print(f"index declares {len(declared)} sitemaps")

        targets = [u for u in declared if "products" in u]
        if args.probe_part2:
            # Undeclared parts are a real possibility, so ask for them directly.
            targets += [f"{BASE}/sitemap-products-part{n}.xml" for n in range(2, 6)]

        rows: list[dict] = []
        per_file: dict[str, int] = {}
        for url in targets:
            xml = fetch(s, url)
            if xml is None:
                continue
            name = url.rsplit("/", 1)[-1]
            (snap_dir / name.replace(".xml", f"-{stamp}.xml")).write_bytes(xml)
            got = parse_products(xml)
            per_file[name] = len(got)
            rows.extend(got)
            print(f"  {name}: {len(got):,} products")

    if not rows:
        print("\nNo product URLs parsed.", file=sys.stderr)
        return 2

    df = pd.DataFrame(rows).drop_duplicates(subset="product_id")
    out = args.out_dir / "catalogue.parquet"
    df.to_parquet(out, index=False)

    suffix_011 = int((df["product_id"].str.endswith("011")).sum())
    report = {
        "products": len(df),
        "per_file": per_file,
        "ids_ending_011": suffix_011,
        "ids_not_ending_011": len(df) - suffix_011,
        "with_image": int((df["image_url"] != "").sum()),
        "with_weight_in_slug": int(df["weight_value"].notna().sum()),
        "possibly_truncated": any(n >= TRUNCATION_WARN_AT for n in per_file.values()),
        "url_cap": SITEMAP_URL_CAP,
    }
    (args.out_dir / "catalogue_stats.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))

    if report["possibly_truncated"]:
        print("\nA file is at the 50k cap — rerun with --probe-part2.", file=sys.stderr)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
