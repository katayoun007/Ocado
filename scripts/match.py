"""Phase 2 — match input products to Ocado catalogue entries.

Produces a ranked candidate set per input product, never a bare winner. Ocado
reuses slugs across genuinely different products (1,897 slugs cover 4,194
catalogue rows), so slug space cannot establish product identity on its own.
Exact identity is settled in verify.py against the fetched page.

Weight is deliberately not used to filter candidates here: only 3.2% of Ocado
slugs carry a pack size, so comparing against the slug would either never fire
or flag everything. It is carried through and spent at the verification step.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).parent))
from canon import slugify  # noqa: E402

# A leading n-gram has to recur before we will believe it names a brand.
BRAND_MIN_COUNT = 5
BRAND_MAX_TOKENS = 4

# Section 6.6. The gap rule matters as much as the threshold: a 95 sitting next
# to a 94 means the algorithm cannot separate two variants.
ACCEPT_SCORE = 92.0
ACCEPT_GAP = 8.0

# This stage does not decide anything, so it should not reject anything either.
# Slug similarity cannot tell "Regal Original Cake Rusks" (59.3) from a genuine
# miss, but verify.py compares real names and real pack sizes and can. So the
# gate is set for recall and precision is bought later, on evidence.
#
# 75 was the plan's figure and it cut real matches; 60 still cut Regal at 59.3
# and Caputo at 59.0. Volume is held down by the brand-absence rule instead,
# which is a fact about the catalogue rather than a guess about a score.
REVIEW_SCORE = 45.0

TOP_N = 3

# IDF scoring is Python-level, so it only ever sees a shortlist. rapidfuzz does
# the sweep over all 49,735 slugs in C++ and hands back the plausible few.
SHORTLIST = 60
SHORTLIST_CUTOFF = 45.0


def build_brand_vocab(slugs: list[str]) -> dict[str, int]:
    """Derive brand names empirically from leading n-grams (section 6.2)."""
    counts: Counter[str] = Counter()
    for slug in slugs:
        parts = slug.split("-")
        for n in range(1, min(BRAND_MAX_TOKENS, len(parts)) + 1):
            counts["-".join(parts[:n])] += 1
    return {ngram: c for ngram, c in counts.items() if c >= BRAND_MIN_COUNT}


def longest_brand(slug: str, vocab: dict[str, int]) -> str:
    """Prefer the longest match so 'm-s-collection' wins over 'm-s'."""
    parts = slug.split("-")
    for n in range(min(BRAND_MAX_TOKENS, len(parts)), 0, -1):
        candidate = "-".join(parts[:n])
        if candidate in vocab:
            return candidate
    return ""


def build_idf(slugs: list[str]) -> dict[str, float]:
    """Rare tokens carry the signal; 'organic' and 'with' carry almost none."""
    df: Counter[str] = Counter()
    for slug in slugs:
        df.update(set(slug.split("-")))
    n = len(slugs)
    return {tok: math.log(n / (1 + count)) for tok, count in df.items()}


def idf_score(a: list[str], b: list[str], idf: dict[str, float]) -> float:
    """IDF-weighted overlap, normalised against the input side.

    Normalising by the input rather than the union stops a long Ocado slug from
    being penalised for carrying extra descriptive words.
    """
    sa, sb = set(a), set(b)
    if not sa:
        return 0.0
    default = max(idf.values(), default=1.0)
    shared = sum(idf.get(t, default) for t in sa & sb)
    total = sum(idf.get(t, default) for t in sa)
    return 100.0 * shared / total if total else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalogue", default=Path("F:/Ocado/data/catalogue.parquet"), type=Path)
    ap.add_argument("--input", default=Path("F:/Ocado/data/input_consumable.csv"), type=Path)
    ap.add_argument("--out-dir", default=Path("F:/Ocado/data"), type=Path)
    args = ap.parse_args()

    cat = pd.read_parquet(args.catalogue)
    slugs = cat.slug.tolist()
    print(f"catalogue: {len(cat):,}")

    vocab = build_brand_vocab(slugs)
    idf = build_idf(slugs)
    print(f"brand vocabulary: {len(vocab):,} n-grams (min count {BRAND_MIN_COUNT})")

    pd.DataFrame(
        sorted(vocab.items(), key=lambda kv: -kv[1]), columns=["brand_ngram", "count"]
    ).to_csv(args.out_dir / "brand_vocab.csv", index=False)

    # Slugs appearing more than once are different products wearing the same
    # name — always send those to verification regardless of score.
    ambiguous_slugs = set(cat.slug[cat.slug.duplicated(keep=False)])

    # If a brand's leading token appears nowhere in the catalogue, Ocado does not
    # stock the brand and no score is going to change that. Separating these
    # keeps the review queue about products that might actually be findable —
    # the three-way outcome of section 5.3, not a two-way one.
    catalogue_first_tokens = {s.split("-")[0] for s in slugs}

    cat_tokens = [s.split("-") for s in slugs]

    # Built once. Rebuilding a brand's slug list per input row was what made the
    # first version quadratic.
    by_brand: dict[str, list[int]] = {}
    for i, s in enumerate(slugs):
        b = longest_brand(s, vocab)
        if b:
            by_brand.setdefault(b, []).append(i)
    brand_slugs = {b: [slugs[i] for i in idx] for b, idx in by_brand.items()}

    rows = list(csv.DictReader(args.input.open(encoding="utf-8")))
    print(f"input: {len(rows):,}\n")

    results = []
    for row in rows:
        in_slug = row["slug_core"]
        in_tokens = in_slug.split("-") if in_slug else []
        brand = longest_brand(in_slug, vocab)

        # Blocking is for accuracy, not speed (section 6.7): a smaller candidate
        # pool is a smaller surface for a confident wrong answer.
        blocked = bool(brand) and brand in by_brand
        haystack = brand_slugs[brand] if blocked else slugs
        index_map = by_brand[brand] if blocked else None

        def rank(haystack: list[str], index_map: list[int] | None) -> list[tuple[float, int]]:
            shortlist = process.extract(
                in_slug, haystack,
                scorer=fuzz.token_set_ratio,
                limit=SHORTLIST,
                score_cutoff=SHORTLIST_CUTOFF,
            )
            out = []
            for _, s_fuzz, pos in shortlist:
                i = index_map[pos] if index_map is not None else pos
                s_idf = idf_score(in_tokens, cat_tokens[i], idf)
                out.append((0.65 * s_idf + 0.35 * s_fuzz, i))
            out.sort(key=lambda t: -t[0])
            return out

        scored = rank(haystack, index_map)

        # "Longest brand match" can swallow a product line — 'bold-bean-co-queen'
        # is not a brand — and then block away the right answer. If the block
        # produced nothing worth reviewing, the block itself is the suspect.
        fell_back = False
        if blocked and (not scored or scored[0][0] < REVIEW_SCORE):
            unblocked = rank(slugs, None)
            if unblocked and (not scored or unblocked[0][0] > scored[0][0]):
                scored, fell_back = unblocked, True

        top = scored[:TOP_N]

        best = top[0][0] if top else 0.0
        second = top[1][0] if len(top) > 1 else 0.0
        gap = best - second

        brand_known = bool(in_tokens) and in_tokens[0] in catalogue_first_tokens

        forced = bool(top) and slugs[top[0][1]] in ambiguous_slugs
        if not top or best < REVIEW_SCORE:
            tier = "not_stocked" if not brand_known else "rejected"
        elif best >= ACCEPT_SCORE and gap >= ACCEPT_GAP and not forced:
            tier = "auto"
        else:
            tier = "review"

        out = {
            "sku": row["sku"],
            "name": row["name"],
            "input_slug": in_slug,
            "brand_detected": brand,
            "blocked_by_brand": blocked and not fell_back,
            "brand_block_fell_back": fell_back,
            "weight_value": row["weight_value"],
            "weight_unit": row["weight_unit"],
            "pack_count": row["pack_count"],
            "tier": tier,
            "brand_in_catalogue": brand_known,
            "best_score": round(best, 1),
            "gap": round(gap, 1),
            "slug_is_ambiguous": forced,
        }
        for rank, (score, i) in enumerate(top, 1):
            out[f"cand{rank}_score"] = round(score, 1)
            out[f"cand{rank}_id"] = cat.product_id.iloc[i]
            out[f"cand{rank}_slug"] = slugs[i]
            out[f"cand{rank}_url"] = cat.url.iloc[i]
        for rank in range(len(top) + 1, TOP_N + 1):
            for suffix in ("score", "id", "slug", "url"):
                out[f"cand{rank}_{suffix}"] = ""
        results.append(out)

    df = pd.DataFrame(results)
    df.to_csv(args.out_dir / "candidates.csv", index=False, encoding="utf-8")
    df[df.tier == "review"].to_csv(args.out_dir / "review_queue.csv", index=False, encoding="utf-8")

    stats = {
        "input": len(df),
        "auto": int((df.tier == "auto").sum()),
        "review": int((df.tier == "review").sum()),
        "rejected": int((df.tier == "rejected").sum()),
        "not_stocked": int((df.tier == "not_stocked").sum()),
        "brand_detected": int((df.brand_detected != "").sum()),
        "blocked_by_brand": int(df.blocked_by_brand.sum()),
        "brand_block_fell_back": int(df.brand_block_fell_back.sum()),
        "forced_by_ambiguous_slug": int(df.slug_is_ambiguous.sum()),
    }
    (args.out_dir / "match_stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
