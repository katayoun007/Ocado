"""Phase 5 — join, check, and write the deliverables.

Two outputs on purpose: JSONL keeps the nested shape so a field wanted three
weeks from now needs no recrawl, CSV is what a person opens.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

CONTENT_FIELDS = [
    "product_information", "features", "country_of_origin", "nutritional_data",
    "storage", "preparation_and_usage", "package_type", "ingredients",
    "allergen_information", "dietary_information", "cooking_guidelines",
]

# Statuses where the Ocado page describes the same product, so its content
# applies. Pack size differs in the second one, which matters for weight-derived
# figures but not for ingredients, allergens or per-100g nutrition.
USABLE = {"confirmed", "probable_size_unknown", "same_product_other_pack"}

FLAT_LIMIT = 4000


def flatten(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:FLAT_LIMIT]
    if isinstance(value, list):
        return " | ".join(str(v) for v in value)[:FLAT_LIMIT]
    if isinstance(value, dict):
        if "markdown" in value:
            return str(value.get("markdown", ""))[:FLAT_LIMIT]
        return json.dumps(value, ensure_ascii=False)[:FLAT_LIMIT]
    return str(value)[:FLAT_LIMIT]


def quality_checks(record: dict) -> list[str]:
    flags = []

    ingredients = record.get("ingredients") or {}
    if isinstance(ingredients, dict) and ingredients.get("text"):
        # The single most important check. If the emphasis markup changes, the
        # output still looks entirely reasonable and is entirely wrong.
        if not ingredients.get("emphasised"):
            flags.append("ingredients_without_emphasis")
        else:
            allergen = (record.get("allergen_information") or {})
            allergen_text = (allergen.get("text") if isinstance(allergen, dict) else "") or ""
            if allergen_text:
                missing = [t for t in ingredients["emphasised"]
                           if t.lower() not in allergen_text.lower()]
                if missing:
                    flags.append(f"allergen_mismatch:{','.join(missing[:4])}")

    nutrition = record.get("nutritional_data")
    if isinstance(nutrition, dict):
        for row in nutrition.get("rows", []):
            for column, parsed in (row.get("parsed") or {}).items():
                if "100" not in column:
                    continue
                for measure in parsed:
                    # More than 100g of anything inside 100g means the value was
                    # read out of the wrong column.
                    if measure.get("unit") == "g" and measure.get("value", 0) > 100:
                        flags.append(f"nutrition_out_of_range:{row.get('label')}")
                        break

    if record.get("input_name") and record.get("ocado_name"):
        if fuzz.token_set_ratio(record["input_name"], record["ocado_name"]) < 60:
            flags.append("title_mismatch")

    return flags


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verified", default=Path("F:/Ocado/data/verified.csv"), type=Path)
    ap.add_argument("--pages", default=Path("F:/Ocado/out/products.jsonl"), type=Path)
    ap.add_argument("--input", default=Path("F:/Ocado/data/input_consumable_instock.csv"), type=Path)
    ap.add_argument("--out-dir", default=Path("F:/Ocado/out"), type=Path)
    args = ap.parse_args()

    pages = {}
    with args.pages.open(encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            pages[str(record["product_id"])] = record

    inputs = {r["sku"]: r for r in csv.DictReader(args.input.open(encoding="utf-8"))}
    verified = pd.read_csv(args.verified, dtype=str).fillna("")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    rows, flat = [], []

    for _, v in verified.iterrows():
        if v.verify_status not in USABLE:
            continue
        page = pages.get(str(v.ocado_product_id))
        if not page:
            continue
        source = inputs.get(v.sku, {})

        record = {
            # LemonSalt identity, so the row can be joined back to Shopify.
            "sku": v.sku,
            "shopify_product_id": source.get("shopify_product_id", ""),
            "shopify_variant_id": source.get("shopify_variant_id", ""),
            "input_name": v["name"],
            "input_size": v.input_size,
            "input_price_gbp": source.get("price_gbp", ""),
            # Ocado identity and provenance.
            "ocado_product_id": v.ocado_product_id,
            "ocado_url": v.ocado_url,
            "ocado_name": v.ocado_name,
            "ocado_size": v.ocado_size,
            "verify_status": v.verify_status,
            "name_score": v.name_score,
        }
        for field in CONTENT_FIELDS:
            record[field] = page.get(field)

        record["quality_flags"] = quality_checks(record)
        rows.append(record)

        flat.append({
            **{k: v for k, v in record.items() if k not in CONTENT_FIELDS + ["quality_flags"]},
            **{f: flatten(record.get(f)) for f in CONTENT_FIELDS},
            "quality_flags": "; ".join(record["quality_flags"]),
        })

    jsonl_path = args.out_dir / "ocado_content.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for record in rows:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    csv_path = args.out_dir / "ocado_content.csv"
    pd.DataFrame(flat).to_csv(csv_path, index=False, encoding="utf-8-sig")

    coverage = {
        f: {"present": sum(1 for r in rows if r.get(f)),
            "missing_pct": round(100 * (1 - sum(1 for r in rows if r.get(f)) / len(rows)), 1)}
        for f in CONTENT_FIELDS
    } if rows else {}

    flag_counts: dict[str, int] = {}
    for r in rows:
        for flag in r["quality_flags"]:
            flag_counts[flag.split(":")[0]] = flag_counts.get(flag.split(":")[0], 0) + 1

    report = {"products": len(rows), "coverage": coverage, "quality_flags": flag_counts}
    (args.out_dir / "quality_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"{len(rows)} products -> {jsonl_path.name}, {csv_path.name}\n")
    print(f"{'field':<24} {'present':>8} {'missing':>9}")
    for field, stat in coverage.items():
        warn = "  <-- check heading text" if stat["missing_pct"] > 20 else ""
        print(f"{field:<24} {stat['present']:>8} {stat['missing_pct']:>8}%{warn}")
    print(f"\nquality flags: {json.dumps(flag_counts) if flag_counts else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
