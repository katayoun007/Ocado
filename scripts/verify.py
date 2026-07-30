"""Phase 4b — decide which candidate is actually the same product.

Slug matching cannot settle identity: Ocado gives genuinely different products
the same slug, and its slugs almost never carry a pack size. The page does carry
one, in schema.org `size`, so the decision happens here against the fetched page
rather than against the sitemap.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
from canon import extract_quantities, slugify, strip_quantities, words  # noqa: E402
from sheet import write_sheet  # noqa: E402

NAME_CONFIRM = 80.0
NAME_POSSIBLE = 60.0

# Pack sizes are quoted, not measured, so allow for rounding between
# "1 litre" and "1000ml" without letting 250g pass as 300g.
WEIGHT_TOLERANCE = 0.02

# Legal-form words that name no brand at all.
BRAND_STOPWORDS = {"and", "of", "ltd", "limited", "plc"}

# A brand token shared by this many distinct brands identifies none of them.
# "Ahmad Tea" was confirmed against "Brew Tea Co" on the strength of the word
# "tea", which eight different brands use. Derived from the brands the pages
# themselves state, so it needs no hand-maintained list.
GENERIC_BRAND_AT = 8

# Words that name a distinct formulation. Strong evidence but not proof: Ocado
# sometimes spells out a claim the input omits ("Less Salt Soy Sauce" against
# "Less Salt Soy Sauce 43% Reduced Salt"), so these cap the verdict at review
# rather than rejecting outright.
VARIANT_MARKERS = {
    "diet", "light", "lite", "zero", "decaf", "decaffeinated", "organic",
    "vegan", "unsalted", "unsweetened", "reduced", "alcohol", "free",
    "wholemeal", "wholegrain", "skimmed",
}

# Axes where both sides state a value and the values disagree. "S&B Golden Curry
# Mix Mild" and "S&B Golden Curry Mix Hot" share brand, share pack size and
# share nine tenths of their name, and are not the same product. Unlike the
# markers above there is no innocent reading of this, so it does reject.
VARIANT_AXES = [
    {"mild", "medium", "hot", "extra"},
    {"salted", "unsalted"},
    {"smooth", "crunchy"},
    {"ground", "whole", "crushed", "sliced", "chopped"},
    # One colour axis, not two. Split into "chocolate" and "everything else",
    # "Yutaka White Roasted Sesame Seeds" and "Yutaka Roasted Black Sesame
    # Seeds" fell either side of the seam and read as agreeing.
    {"milk", "dark", "plain", "white", "black", "red", "green", "yellow", "brown"},
]

# Rarity is recorded to help whoever works the review queue, but it is not what
# decides. Measuring it showed why: the words that actually fork a product line
# are usually common ones — "Fig, Apple & Garlic Chutney" against "Peach & Mango
# Chutney", "Mushroom Soy Sauce" against "Fish Sauce" — while genuinely
# identical pairs were separated by rare ones, a parent brand ("Nestle
# Carnation" / "Carnation") or a spelling ("Lemonade" / "Limonade").
#
# What separates them is the shape of the disagreement, not the rarity. See
# divergence() below.
RARE_TOKEN_IDF = 7.5

PER_PACK = re.compile(r"\b(\d+)\s*per\s*pack\b", re.I)


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


def name_tokens(text: str) -> set[str]:
    """Tokens with the pack size taken out.

    The input name carries "(370g)" and the Ocado name never does, so scoring
    the raw strings penalises every input for a token the other side cannot
    have — worth a systematic 5 points across the whole set.
    """
    slug = slugify(strip_quantities(text or ""))
    return set(slug.split("-")) - {""}


def joined(text: str) -> str:
    """Slug with the separators taken out.

    Every apostrophe, full stop and space in a name becomes a "-", so the same
    words tokenise differently on the two sides: "Lingham's" splits into
    lingham + s while "Linghams" stays whole, and "Soy Bean" never equals
    "Soybean". Containment against this form sees through all of it.
    """
    return slugify(text or "").replace("-", "")


def stated_brand(page: dict) -> str:
    return page.get("ocado_brand") or page.get("brand") or ""


def brand_agreement(input_name: str, page: dict, generic: set[str]) -> str:
    """Does the brand the page states appear in the input name at all?

    Calibrated against the matches we already believed: of 119 confirmed, only
    two disagreed on brand, and both of those were wrong — "Saloio Extra Virgin
    Olive Oil" had been matched to El Molino's, on the strength of four shared
    descriptive words. So brand disagreement is not a low score to be weighed
    against a high one. It is a different product.
    """
    brand = {t for t in slugify(stated_brand(page)).split("-")
             if t and t not in BRAND_STOPWORDS}
    if not brand:
        return "unknown"

    if (brand - generic) & name_tokens(input_name):
        return "agree"

    # Written differently on the two sides: "Lingham's" against "Linghams". Only
    # for brands long enough to mean something as a substring — "M&S" reduces to
    # "ms", which is inside "bodrumsunfloweroil".
    flat = joined(stated_brand(page))
    if len(flat) >= 4 and flat in joined(input_name):
        return "agree"

    # Ocado credits the distributor as the brand and leaves the real one in the
    # name — "Brindisa Ortiz Yellowfin Tuna", "Casa Firelli". The input's own
    # leading word appearing in the page name is the same brand seen from the
    # other end.
    lead = next(iter(slugify(strip_quantities(input_name)).split("-")), "")
    if len(lead) > 2 and lead in name_tokens(page.get("ocado_name", "")):
        return "agree"

    return "disagree"


def respelled(token: str, other: str) -> bool:
    """Is this token present in the other side, just written differently?

    Covers the separator ("Sugarfree" for "Sugar Free", "No.66" for "No66") and
    the plural ("Cocktail" for "Cocktails"). `other` must be the opposite side
    alone: tested against both sides concatenated, every token trivially matches
    itself and the check silently does nothing.
    """
    return (token in other
            or (token.endswith("s") and token[:-1] in other)
            or f"{token}s" in other)


def one_sided(a: set[str], b: set[str], ja: str, jb: str) -> set[str]:
    """Tokens genuinely present on one side only."""
    return ({t for t in a - b if not respelled(t, jb)}
            | {t for t in b - a if not respelled(t, ja)})


def variant_conflict(input_name: str, page: dict) -> tuple[str, str]:
    """Same brand, same size, different thing on the shelf.

    Returns (kind, detail). "axis" rejects the candidate, "marker" only bars it
    from being confirmed.
    """
    a = name_tokens(input_name)
    b = name_tokens(page.get("ocado_name", ""))

    for axis in VARIANT_AXES:
        va, vb = a & axis, b & axis
        if va and vb and va != vb:
            return "axis", f"{'/'.join(sorted(va))} vs {'/'.join(sorted(vb))}"

    markers = one_sided(a, b, joined(input_name),
                        joined(page.get("ocado_name", ""))) & VARIANT_MARKERS
    if markers:
        return "marker", ",".join(sorted(markers))
    return "", ""


def rare_divergence(input_name: str, page: dict, idf: dict[str, float],
                    ceiling: float) -> str:
    """The rarest word present on one side and absent from the other."""
    ja, jb = joined(input_name), joined(page.get("ocado_name", ""))
    a, b = name_tokens(input_name), name_tokens(page.get("ocado_name", ""))

    worst, score = "", 0.0
    for token in one_sided(a, b, ja, jb):
        # Numbers and initials distinguish nothing on their own.
        if token.isdigit() or len(token) <= 2:
            continue
        value = idf.get(token, ceiling)
        if value > score:
            worst, score = token, value
    return worst if score >= RARE_TOKEN_IDF else ""


def divergence(input_name: str, page: dict, idf: dict[str, float],
               ceiling: float) -> str:
    """Do the two names disagree about what the thing is?

    Both sides carrying a word the other lacks is a fork in the product line,
    however ordinary the words: "Anna's Cappuccino Biscuit Thins" scores 80
    against "Anna's Almond Biscuit" and is a different biscuit.

    One side carrying extra words is usually just the fuller description, and
    which side matters. Ocado elaborating is harmless — "Serious Pig Snacking
    Pickles" is "Serious Pig Snacking Pickles Crunchy Tangy Mini Gherkins". The
    input naming something distinctive that appears nowhere on the page is not,
    because that word may be the product: "Rummo Mezze Penne Rigate" is a
    different shape from "Rummo Penne Rigate No. 66".
    """
    ja, jb = joined(input_name), joined(page.get("ocado_name", ""))
    a, b = name_tokens(input_name), name_tokens(page.get("ocado_name", ""))

    def content(tokens: set[str], other: str) -> set[str]:
        return {t for t in tokens
                if not t.isdigit() and len(t) > 2 and not respelled(t, other)}

    mine, theirs = content(a - b, jb), content(b - a, ja)
    if mine and theirs:
        return f"{'/'.join(sorted(mine)[:3])} vs {'/'.join(sorted(theirs)[:3])}"
    if mine:
        rare = sorted(t for t in mine if idf.get(t, ceiling) >= RARE_TOKEN_IDF)
        if rare:
            return f"{'/'.join(rare[:3])} not on the page"
    return ""


def input_size(row: pd.Series) -> str:
    """Readable pack size, or empty. Formatting the raw fields concatenated two
    NaNs into the literal string "nannan" for every sizeless product."""
    value, unit = row.get("weight_value") or "", row.get("weight_unit") or ""
    if value and unit and str(value).lower() != "nan":
        return f"{float(value):g}{unit}"
    count = row.get("pack_count") or ""
    if count and str(count).lower() != "nan":
        return f"{int(float(count))} pack"
    return ""


def count_of(text: str) -> int | None:
    """Ocado writes multipacks as "40 per pack", which the shared count pattern
    does not reach because of the intervening word."""
    q = extract_quantities(text or "")
    if q["count"]:
        return q["count"]
    m = PER_PACK.search(text or "")
    return int(m.group(1)) if m else None


def size_verdict(row: pd.Series, page: dict) -> str:
    """Compare the input pack size against the page's stated size.

    Weight first, then pack count. Tea bags and coffee pods are sold by the
    count and carry no weight at all, so without the fallback a 300-bag box and
    a 40-bag box look equally plausible.
    """
    try:
        want_value = float(row.get("weight_value") or "")
    except (TypeError, ValueError):
        want_value = None
    want_unit = str(row.get("weight_unit") or "")

    got = weight_of(page.get("ocado_size", "")) or weight_of(page.get("ocado_name", ""))

    if want_value is not None and want_unit and got is not None:
        got_unit, got_value = got
        if got_unit != want_unit:
            return "unit_mismatch"
        if abs(got_value - want_value) <= WEIGHT_TOLERANCE * max(got_value, want_value):
            return "match"
        return "mismatch"

    try:
        want_count = int(float(row.get("pack_count") or ""))
    except (TypeError, ValueError):
        want_count = None
    got_count = count_of(page.get("ocado_size", "")) or count_of(page.get("ocado_name", ""))
    if want_count and got_count:
        return "match" if want_count == got_count else "mismatch"

    if want_value is None or not want_unit:
        return "input_has_no_size"
    return "page_has_no_size"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", default=Path("F:/Ocado/data/candidates.csv"), type=Path)
    ap.add_argument("--pages", default=Path("F:/Ocado/out/products.jsonl"), type=Path)
    ap.add_argument("--catalogue", default=Path("F:/Ocado/data/catalogue.parquet"), type=Path)
    ap.add_argument("--out-dir", default=Path("F:/Ocado/data"), type=Path)
    args = ap.parse_args()

    pages = load_pages(args.pages)
    df = pd.read_csv(args.candidates, dtype=str)
    df = df[df.tier.isin({"auto", "review"})]

    # Which words are rare enough to distinguish two products, measured against
    # the catalogue rather than assumed.
    slugs = pd.read_parquet(args.catalogue).slug.tolist()
    document_freq: Counter[str] = Counter()
    for slug in slugs:
        document_freq.update(set(slug.split("-")))
    idf = {t: math.log(len(slugs) / (1 + c)) for t, c in document_freq.items()}
    ceiling = math.log(len(slugs))

    # Which brand words are too widely shared to identify a brand.
    brand_freq: Counter[str] = Counter()
    for brand in {stated_brand(p) for p in pages.values() if stated_brand(p)}:
        brand_freq.update(set(slugify(brand).split("-")) - {""})
    generic = {t for t, n in brand_freq.items() if n >= GENERIC_BRAND_AT}

    print(f"pages parsed {len(pages):,} | products to settle {len(df):,}")
    print(f"generic brand words: {', '.join(sorted(generic))}\n")

    results = []
    for _, row in df.iterrows():
        assessed = []
        for rank in (1, 2, 3):
            pid = str(row.get(f"cand{rank}_id") or "").strip()
            pid = pid[:-2] if pid.endswith(".0") else pid
            page = pages.get(pid)
            if not page:
                continue

            variant_kind, variant_detail = variant_conflict(row["name"], page)
            stripped = words(strip_quantities(row["name"]))
            name_score = max(
                fuzz.token_set_ratio(
                    stripped, words(strip_quantities(page.get("ocado_name", "")))),
                fuzz.token_set_ratio(
                    stripped, words(strip_quantities(page.get("ocado_title", "")))),
            )
            assessed.append({
                "rank": rank,
                "product_id": pid,
                "url": row.get(f"cand{rank}_url"),
                "ocado_name": page.get("ocado_name", ""),
                "ocado_size": page.get("ocado_size", ""),
                "ocado_brand": page.get("ocado_brand") or page.get("brand") or "",
                "name_score": round(name_score, 1),
                "size": size_verdict(row, page),
                "brand": brand_agreement(row["name"], page, generic),
                "variant_kind": variant_kind,
                "variant": variant_detail,
                "rare_word": rare_divergence(row["name"], page, idf, ceiling),
                "divergence": divergence(row["name"], page, idf, ceiling),
            })

        # Brand is identity, not evidence to be weighed, and a contradicted
        # variant axis is the same. Both are settled before any score is read.
        eligible = [a for a in assessed
                    if a["brand"] != "disagree" and a["variant_kind"] != "axis"]

        # A stated size that disagrees is decisive — same words, different pack
        # is a different product.
        alive = [a for a in eligible if a["size"] != "mismatch" and a["size"] != "unit_mismatch"]
        alive.sort(key=lambda a: -a["name_score"])

        # Names disagreeing about what the thing is, or a one-sided formulation
        # word, is not enough to reject on — "Rio Mare Insalatissime Mexican
        # Style Tuna Salad" and Ocado's "Rio Mare MSC Tuna Salad Mexican Style"
        # diverge both ways and are the same tin. It is enough to withhold
        # "confirmed" and put a person in front of the pair.
        def clean(a: dict) -> bool:
            return not a["variant_kind"] and not a["divergence"]

        confirmed = [a for a in alive
                     if a["name_score"] >= NAME_CONFIRM and a["size"] == "match" and clean(a)]
        probable = [a for a in alive if a["name_score"] >= NAME_CONFIRM and clean(a)]
        possible = [a for a in alive if a["name_score"] >= NAME_POSSIBLE]

        if len(confirmed) == 1:
            status, pick = "confirmed", confirmed[0]
        elif confirmed:
            status, pick = "ambiguous", confirmed[0]
        elif len(probable) == 1:
            status, pick = "probable_size_unknown", probable[0]
        elif possible:
            status, pick = "needs_review", possible[0]
        elif eligible:
            # Every eligible candidate failed on size. Whether that matters
            # depends on why: a different pack of the same product carries
            # identical ingredients, allergens and per-100g nutrition, while a
            # different product carries none of it. Same bucket, opposite
            # usefulness.
            rejected = sorted(eligible, key=lambda a: -a["name_score"])
            if rejected[0]["name_score"] >= NAME_CONFIRM and clean(rejected[0]):
                status = "same_product_other_pack"
            else:
                status = "different_product"
            pick = rejected[0]
        elif assessed:
            # Nothing survived brand or formulation. Naming the reason keeps the
            # two apart in the stats: a wrong brand means the matcher reached for
            # a rival's product, a variant clash means it got the range right and
            # the item wrong.
            rejected = sorted(assessed, key=lambda a: -a["name_score"])
            status = ("different_brand" if rejected[0]["brand"] == "disagree"
                      else "different_variant")
            pick = rejected[0]
        else:
            status, pick = "no_page", None

        out = {
            "sku": row["sku"],
            "shopify_product_id": row.get("shopify_product_id", ""),
            "name": row["name"],
            "input_size": input_size(row),
            "verify_status": status,
            "match_score": row["best_score"],
        }
        if pick:
            out.update({
                "ocado_product_id": pick["product_id"],
                "ocado_url": pick["url"],
                "ocado_name": pick["ocado_name"],
                "ocado_size": pick["ocado_size"],
                "ocado_brand": pick["ocado_brand"],
                "name_score": pick["name_score"],
                "size_verdict": pick["size"],
                "brand_verdict": pick["brand"],
                "variant_conflict": pick["variant"],
                "divergence": pick["divergence"],
                "rare_word": pick["rare_word"],
            })
        out["alternatives"] = json.dumps(
            [{k: a[k] for k in ("product_id", "ocado_name", "ocado_size", "ocado_brand",
                                "name_score", "size", "brand", "variant", "divergence")}
             for a in assessed if not pick or a["product_id"] != pick["product_id"]],
            ensure_ascii=False)
        results.append(out)

    out_df = pd.DataFrame(results)
    out_df.to_csv(args.out_dir / "verified.csv", index=False, encoding="utf-8")
    out_df[out_df.verify_status == "confirmed"].to_csv(
        args.out_dir / "verified_confirmed.csv", index=False, encoding="utf-8")
    # Only the products a person can actually settle. Writing everything that
    # was not confirmed put 751 wrong-brand pages in a queue titled "review",
    # where they were 55% of the work and none of the decisions.
    review = out_df[out_df.verify_status.isin({"needs_review", "ambiguous"})].copy()
    review.to_csv(args.out_dir / "verify_review_queue.csv", index=False, encoding="utf-8")

    # Same queue as a workbook, because this is the one output a person works
    # through by hand. "decision" is left empty to be filled in.
    review.insert(0, "decision", "")
    write_sheet(args.out_dir / "verify_review_queue.xlsx", review, "review", [
        "decision", "name", "ocado_name", "input_size", "ocado_size",
        "divergence", "variant_conflict", "rare_word", "name_score", "ocado_url",
    ])

    # Everything ruled out, kept separately so the rejections stay auditable.
    out_df[out_df.verify_status.str.startswith("different")].to_csv(
        args.out_dir / "verify_rejected.csv", index=False, encoding="utf-8")

    counts = out_df.verify_status.value_counts().to_dict()
    (args.out_dir / "verify_stats.json").write_text(
        json.dumps(counts, indent=2), encoding="utf-8")
    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
