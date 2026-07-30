"""Canonical forms shared by the input prep and the matcher.

Section 6.1 of the plan: a slug is a lossy transform of a product name, so we
never invert it. We push both sides through the same transform and compare in
slug space.
"""

from __future__ import annotations

import re
import unicodedata

# Ocado slugs collapse anything non-alphanumeric to "-", which is why "M&S" and
# "Neal's" both produce the same shape. We reproduce that exactly.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Quantity patterns (section 6.5). Ordered longest-first so "1.5kg" wins over "5kg".
_UNIT = r"(?:kg|g|mg|ml|cl|l|litres?|ltr|oz|lbs?)"
_WEIGHT = re.compile(rf"\b(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.I)
_MULTIPACK = re.compile(rf"\b(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*({_UNIT})\b", re.I)
_COUNT = re.compile(r"\b(\d+)\s*(?:x|pack|pk|pcs?|pieces?|ct)\b", re.I)
_AGE = re.compile(r"\b(\d+)\s*-\s*(\d+)\s*years?\b", re.I)
_SIZE = re.compile(r"\bsize\s*-?\s*(\d+)\b", re.I)

# Units normalised to a base so "1.5kg" and "1500g" compare equal.
_TO_BASE = {
    "mg": ("g", 0.001),
    "g": ("g", 1.0),
    "kg": ("g", 1000.0),
    "oz": ("g", 28.3495),
    "lb": ("g", 453.592),
    "lbs": ("g", 453.592),
    "ml": ("ml", 1.0),
    "cl": ("ml", 10.0),
    "l": ("ml", 1000.0),
    "litre": ("ml", 1000.0),
    "litres": ("ml", 1000.0),
    "ltr": ("ml", 1000.0),
}


def slugify(text: str) -> str:
    """Apply the same lossy transform Ocado applies when building a slug."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = _NON_ALNUM.sub("-", text)
    return text.strip("-")


def tokens(text: str) -> list[str]:
    slug = slugify(text)
    return slug.split("-") if slug else []


def _base(value: float, unit: str) -> tuple[str, float] | None:
    entry = _TO_BASE.get(unit.lower().rstrip("s") if unit.lower() != "s" else unit.lower())
    if entry is None:
        entry = _TO_BASE.get(unit.lower())
    if entry is None:
        return None
    base_unit, factor = entry
    return base_unit, round(value * factor, 4)


def extract_quantities(text: str) -> dict:
    """Pull the quantitative signals out so they can be compared separately.

    Section 6.5: when both sides carry a quantity and they disagree, the
    candidate is eliminated outright rather than merely down-scored.
    """
    out: dict = {"weight": None, "count": None, "age": None, "size": None, "raw": []}
    if not text:
        return out

    # A multipack ("4x160ml") carries both a count and a per-unit weight; total
    # weight is what actually identifies the product.
    m = _MULTIPACK.search(text)
    if m:
        count, value, unit = int(m.group(1)), float(m.group(2)), m.group(3)
        out["count"] = count
        based = _base(value * count, unit)
        if based:
            out["weight"] = based
        out["raw"].append(m.group(0))
    else:
        m = _WEIGHT.search(text)
        if m:
            based = _base(float(m.group(1)), m.group(2))
            if based:
                out["weight"] = based
            out["raw"].append(m.group(0))
        m = _COUNT.search(text)
        if m:
            out["count"] = int(m.group(1))
            out["raw"].append(m.group(0))

    m = _AGE.search(text)
    if m:
        out["age"] = (int(m.group(1)), int(m.group(2)))
        out["raw"].append(m.group(0))

    m = _SIZE.search(text)
    if m:
        out["size"] = int(m.group(1))
        out["raw"].append(m.group(0))

    return out


def strip_quantities(text: str) -> str:
    """Remove quantity fragments so the remaining words can be scored on merit."""
    for pattern in (_MULTIPACK, _WEIGHT, _COUNT, _AGE, _SIZE):
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()
