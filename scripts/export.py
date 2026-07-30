"""Phase 5 — join, check, and write the deliverables.

Three outputs on purpose. JSONL keeps the nested shape so a field wanted three
weeks from now needs no recrawl. XLSX is what a person opens: the content fields
run to paragraphs and contain their own line breaks, and a spreadsheet that
mis-reads one quoted newline shears every following column onto the wrong row —
which is exactly what Google Sheets did to the CSV. XLSX carries the cell
boundaries in the format itself, so there is nothing to mis-detect. The CSV
stays for anything that wants to read it as a stream.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
from sheet import write_sheet  # noqa: E402

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

# The fourteen declarable allergens of EU 1169/2011 Annex II, with the words a
# label actually uses. Only for deciding whether an unemphasised ingredient list
# is suspicious: strawberry jam has nothing to emphasise, so alarming on it
# buries the cases that matter under a hundred that do not.
ALLERGEN_WORDS = {
    "wheat", "rye", "barley", "oats", "oat", "spelt", "kamut", "gluten",
    "crustacean", "prawn", "shrimp", "crab", "lobster",
    "egg", "eggs", "fish", "anchovy", "anchovies", "tuna", "salmon", "cod",
    "peanut", "peanuts", "soy", "soya", "soybean", "soybeans",
    # No bare "butter": butter beans are a legume, and the word also carries
    # peanut, cocoa and shea butter. Milk is named by the other six.
    "milk", "cream", "cheese", "yoghurt", "yogurt", "whey", "lactose",
    "almond", "almonds", "hazelnut", "hazelnuts", "walnut", "walnuts",
    "cashew", "cashews", "pecan", "pecans", "pistachio", "pistachios",
    "macadamia", "brazil", "nut", "nuts",
    "celery", "celeriac", "mustard", "sesame", "sulphite", "sulphites",
    "sulphur", "lupin", "mollusc", "molluscs", "squid", "mussel", "mussels",
    "oyster", "oysters", "clam", "clams", "snail", "snails",
}


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
    return str(value)


def one_line(value: object) -> str:
    """Same as flatten, with the internal line breaks turned into separators.

    A record has to occupy exactly one line for the CSV to survive a reader that
    ignores quoting. The nested JSONL keeps the real breaks.
    """
    text = flatten(value)
    return " / ".join(part.strip() for part in text.splitlines() if part.strip())[:FLAT_LIMIT]


def quality_checks(record: dict) -> list[str]:
    flags = []

    ingredients = record.get("ingredients") or {}
    if isinstance(ingredients, dict) and ingredients.get("text"):
        # The single most important check. If the emphasis markup changes, the
        # output still looks entirely reasonable and is entirely wrong. Restricted
        # to lists that name a declarable allergen, because those are the only
        # ones where finding no emphasis is evidence of anything.
        if not ingredients.get("emphasised"):
            words = set(re.findall(r"[a-z]+", ingredients["text"].lower()))
            hits = sorted(words & ALLERGEN_WORDS)
            if hits:
                flags.append(f"allergen_present_unmarked:{','.join(hits[:4])}")
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


# The columns that answer "is this the same product?", moved to the front so the
# check needs no scrolling.
LEAD_COLUMNS = [
    "input_name", "ocado_name", "input_size", "ocado_size",
    "verify_status", "name_score", "quality_flags", "ocado_url",
]


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
            "ocado_image_main": page.get("ocado_image_main", ""),
            "ocado_images_other": page.get("ocado_images_other") or [],
            "verify_status": v.verify_status,
            "name_score": v.name_score,
        }
        for field in CONTENT_FIELDS:
            record[field] = page.get(field)

        record["quality_flags"] = quality_checks(record)
        rows.append(record)

        flat.append({
            **{k: one_line(v) for k, v in record.items()
               if k not in CONTENT_FIELDS + ["quality_flags"]},
            **{f: one_line(record.get(f)) for f in CONTENT_FIELDS},
            "quality_flags": "; ".join(record["quality_flags"]),
        })

    jsonl_path = args.out_dir / "ocado_content.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for record in rows:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    csv_path = args.out_dir / "ocado_content.csv"
    flat_df = pd.DataFrame(flat)
    flat_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    xlsx_path = args.out_dir / "ocado_content.xlsx"
    write_sheet(xlsx_path, flat_df, "products", LEAD_COLUMNS)

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

    print(f"{len(rows)} products -> {xlsx_path.name}, {jsonl_path.name}, {csv_path.name}\n")
    print(f"{'field':<24} {'present':>8} {'missing':>9}")
    for field, stat in coverage.items():
        warn = "  <-- check heading text" if stat["missing_pct"] > 20 else ""
        print(f"{field:<24} {stat['present']:>8} {stat['missing_pct']:>8}%{warn}")
    print(f"\nquality flags: {json.dumps(flag_counts) if flag_counts else 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
