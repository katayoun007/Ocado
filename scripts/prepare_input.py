"""Phase 0 — turn the Google Ads product report into a clean matching input.

The report is a UTF-16LE, tab-separated export with two preamble lines before
the header row. Item ID is a Shopify composite id, so it carries no Ocado
signal; everything the matcher needs has to come out of the title.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from canon import extract_quantities, slugify, strip_quantities  # noqa: E402

PREAMBLE_LINES = 2
ITEM_ID = re.compile(r"^shopify_(?P<market>[a-z]{2})_(?P<product>\d+)_(?P<variant>\d+)$")

# A dash in the title separates either brand from product ("Lee Kum Kee - Teriyaki
# Sauce") or product from variant ("Jinro Chamisul Soju - Peach"). Nothing in the
# title distinguishes them, so we record both sides as a hint and let the matcher
# decide once it has the brand vocabulary derived from the sitemap (section 6.2).
# Committing here would reduce "Jinro Chamisul Soju - Peach" to "Peach".
TITLE_DASH = re.compile(r"^(?P<left>[^-]{2,40}?)\s+-\s+(?P<right>.+)$")

# Everything Ocado could plausibly hold content fields for.
CONSUMABLE_CATEGORIES = {"Food, Beverages & Tobacco", "Health & Beauty"}


def read_report(path: Path) -> list[dict]:
    raw = path.read_bytes()
    if raw[:2] == b"\xff\xfe":
        text = raw.decode("utf-16-le")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("utf-8")

    lines = text.splitlines()
    body = "\n".join(lines[PREAMBLE_LINES:])
    return list(csv.DictReader(io.StringIO(body), delimiter="\t"))


def parse_price(value: str) -> float | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d.]", "", value)
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def build_row(row: dict) -> dict | None:
    title = (row.get("Title") or "").strip()
    item_id = (row.get("Item ID") or "").strip()
    if not title or not item_id:
        return None

    m = ITEM_ID.match(item_id)
    product_id = m.group("product") if m else ""
    variant_id = m.group("variant") if m else ""

    quantities = extract_quantities(title)

    dash = TITLE_DASH.match(title)
    dash_left = dash.group("left").strip() if dash else ""
    dash_right = dash.group("right").strip() if dash else ""

    # Keep the whole title as the core. The matcher scores on words, so only the
    # quantities and the trailing parenthetical come out here — those are
    # compared separately (section 6.5).
    core = re.sub(r"\([^)]*\)", " ", title)
    core = strip_quantities(core)
    core = re.sub(r"\s+", " ", core).strip(" -,")

    category = (row.get("Category (1st level)") or "").strip().strip('"')

    # Merchant Center reports stock as a newline-separated issue list. This is a
    # snapshot from the report window, not live stock.
    issues = [i.strip() for i in (row.get("Issues") or "").split("\n") if i.strip()]
    out_of_stock = any(i.lower() == "out of stock" for i in issues)

    return {
        "sku": item_id,
        "shopify_product_id": product_id,
        "shopify_variant_id": variant_id,
        "name": title,
        "name_core": core,
        "slug_full": slugify(title),
        "slug_core": slugify(core),
        "dash_left": dash_left,
        "dash_right": dash_right,
        "weight_value": quantities["weight"][1] if quantities["weight"] else "",
        "weight_unit": quantities["weight"][0] if quantities["weight"] else "",
        "pack_count": quantities["count"] or "",
        "quantities_raw": " | ".join(quantities["raw"]),
        "price_gbp": parse_price(row.get("Price", "")),
        "category": category,
        "is_consumable": category in CONSUMABLE_CATEGORIES,
        "status": (row.get("Status") or "").strip(),
        "issues": "; ".join(issues),
        "out_of_stock": out_of_stock,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", required=True, type=Path)
    ap.add_argument("--out-dir", default=Path("F:/Ocado/data"), type=Path)
    args = ap.parse_args()

    rows = [r for r in (build_row(r) for r in read_report(args.report)) if r]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    fields = list(rows[0].keys())
    all_path = args.out_dir / "input_all.csv"
    food_path = args.out_dir / "input_consumable.csv"
    stock_path = args.out_dir / "input_consumable_instock.csv"

    subsets = (
        (all_path, rows),
        (food_path, [r for r in rows if r["is_consumable"]]),
        (stock_path, [r for r in rows if r["is_consumable"] and not r["out_of_stock"]]),
    )
    for path, subset in subsets:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(subset)

    consumable = [r for r in rows if r["is_consumable"]]
    stats = {
        "total": len(rows),
        "consumable": len(consumable),
        "with_dash_hint": sum(1 for r in rows if r["dash_left"]),
        "with_weight": sum(1 for r in rows if r["weight_value"] != ""),
        "with_pack_count": sum(1 for r in rows if r["pack_count"] != ""),
        "consumable_with_weight": sum(1 for r in consumable if r["weight_value"] != ""),
        "out_of_stock": sum(1 for r in rows if r["out_of_stock"]),
        "consumable_out_of_stock": sum(1 for r in consumable if r["out_of_stock"]),
        "consumable_in_stock": sum(1 for r in consumable if not r["out_of_stock"]),
        "unparsed_item_id": sum(1 for r in rows if not r["shopify_product_id"]),
    }
    (args.out_dir / "input_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    print(json.dumps(stats, indent=2))
    print(f"\nwrote {all_path}\nwrote {food_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
