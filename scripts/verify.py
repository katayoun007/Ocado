"""Phase 4b — decide which candidate is actually the same product.

Slug matching cannot settle identity: Ocado gives genuinely different products
the same slug, and its slugs almost never carry a pack size. The page does carry
one, in schema.org `size`, so the decision happens here against the fetched page
rather than against the sitemap.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
from canon import extract_quantities, slugify  # noqa: E402

NAME_CONFIRM = 80.0
NAME_POSSIBLE = 60.0

# Pack sizes are quoted, not measured, so allow for rounding between
# "1 litre" and "1000ml" without letting 250g pass as 300g.
WEIGHT_TOLERANCE = 0.02


def load_pages(path: Path) -> dict[str, dict]:
    pages = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            pages[str(record["product_id"])] = record
    return pages


def weight_of(text: str) -> tuple[str, float] | None:
    q = extract_quantities(text or "")
    return q["weight"]


def size_verdict(row: pd.Series, page: dict) -> str:
    """Compare the input pack size against the page's stated size."""
    try:
        want_value = float(row.get("weight_value") or "")
    except (TypeError, ValueError):
        want_value = None
    want_unit = str(row.get("weight_unit") or "")

    got = weight_of(page.get("ocado_size", "")) or weight_of(page.get("ocado_name", ""))

    if want_value is None or not want_unit:
        return "input_has_no_size"
    if got is None:
        return "page_has_no_size"
    got_unit, got_value = got
    if got_unit != want_unit:
        return "unit_mismatch"
    if abs(got_value - want_value) <= WEIGHT_TOLERANCE * max(got_value, want_value):
        return "match"
    return "mismatch"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", default=Path("F:/Ocado/data/candidates.csv"), type=Path)
    ap.add_argument("--pages", default=Path("F:/Ocado/out/products.jsonl"), type=Path)
    ap.add_argument("--out-dir", default=Path("F:/Ocado/data"), type=Path)
    args = ap.parse_args()

    pages = load_pages(args.pages)
    df = pd.read_csv(args.candidates, dtype=str)
    df = df[df.tier.isin({"auto", "review"})]
    print(f"pages parsed {len(pages):,} | products to settle {len(df):,}\n")

    results = []
    for _, row in df.iterrows():
        assessed = []
        for rank in (1, 2, 3):
            pid = str(row.get(f"cand{rank}_id") or "").strip()
            pid = pid[:-2] if pid.endswith(".0") else pid
            page = pages.get(pid)
            if not page:
                continue

            name_score = max(
                fuzz.token_set_ratio(slugify(row["name"]), slugify(page.get("ocado_name", ""))),
                fuzz.token_set_ratio(slugify(row["name"]), slugify(page.get("ocado_title", ""))),
            )
            assessed.append({
                "rank": rank,
                "product_id": pid,
                "url": row.get(f"cand{rank}_url"),
                "ocado_name": page.get("ocado_name", ""),
                "ocado_size": page.get("ocado_size", ""),
                "name_score": round(name_score, 1),
                "size": size_verdict(row, page),
            })

        # A stated size that disagrees is decisive — same words, different pack
        # is a different product.
        alive = [a for a in assessed if a["size"] != "mismatch" and a["size"] != "unit_mismatch"]
        alive.sort(key=lambda a: -a["name_score"])

        confirmed = [a for a in alive if a["name_score"] >= NAME_CONFIRM and a["size"] == "match"]
        probable = [a for a in alive if a["name_score"] >= NAME_CONFIRM]
        possible = [a for a in alive if a["name_score"] >= NAME_POSSIBLE]

        if len(confirmed) == 1:
            status, pick = "confirmed", confirmed[0]
        elif confirmed:
            status, pick = "ambiguous", confirmed[0]
        elif len(probable) == 1:
            status, pick = "probable_size_unknown", probable[0]
        elif possible:
            status, pick = "needs_review", possible[0]
        elif assessed:
            # Every candidate failed on size. Whether that matters depends on
            # why: a different pack of the same product carries identical
            # ingredients, allergens and per-100g nutrition, while a different
            # product carries none of it. Same bucket, opposite usefulness.
            rejected = sorted(assessed, key=lambda a: -a["name_score"])
            if rejected[0]["name_score"] >= NAME_CONFIRM:
                status = "same_product_other_pack"
            else:
                status = "different_product"
            pick = rejected[0]
        else:
            status, pick = "no_page", None

        out = {
            "sku": row["sku"],
            "shopify_product_id": row.get("shopify_product_id", ""),
            "name": row["name"],
            "input_size": f"{row.get('weight_value') or ''}{row.get('weight_unit') or ''}",
            "verify_status": status,
            "match_score": row["best_score"],
        }
        if pick:
            out.update({
                "ocado_product_id": pick["product_id"],
                "ocado_url": pick["url"],
                "ocado_name": pick["ocado_name"],
                "ocado_size": pick["ocado_size"],
                "name_score": pick["name_score"],
                "size_verdict": pick["size"],
            })
        out["alternatives"] = json.dumps(
            [{k: a[k] for k in ("product_id", "ocado_name", "ocado_size", "name_score", "size")}
             for a in assessed if not pick or a["product_id"] != pick["product_id"]],
            ensure_ascii=False)
        results.append(out)

    out_df = pd.DataFrame(results)
    out_df.to_csv(args.out_dir / "verified.csv", index=False, encoding="utf-8")
    out_df[out_df.verify_status == "confirmed"].to_csv(
        args.out_dir / "verified_confirmed.csv", index=False, encoding="utf-8")
    out_df[~out_df.verify_status.isin({"confirmed"})].to_csv(
        args.out_dir / "verify_review_queue.csv", index=False, encoding="utf-8")

    counts = out_df.verify_status.value_counts().to_dict()
    (args.out_dir / "verify_stats.json").write_text(
        json.dumps(counts, indent=2), encoding="utf-8")
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
