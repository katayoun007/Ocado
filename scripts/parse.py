"""Phase 4 — extract the content fields from cached pages.

Sections are anchored on heading text, never on class names. Ocado's classes may
be hashed, and when a class selector breaks it returns an empty string rather
than raising — leaving 300 records that look like products which simply had no
ingredients. Heading text is the most stable thing on the page.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

# Confirmed present as <h2> by survey_headings.py over the cached pages.
FIELDS: dict[str, tuple[str, str]] = {
    "product information": ("product_information", "text"),
    "features": ("features", "list"),
    "country of origin": ("country_of_origin", "text"),
    "nutritional data": ("nutritional_data", "table"),
    "storage": ("storage", "text"),
    "preparation and usage": ("preparation_and_usage", "text"),
    "package type": ("package_type", "text"),
    "ingredients": ("ingredients", "rich"),
    "allergen information": ("allergen_information", "rich"),
    "dietary information": ("dietary_information", "list"),
    # Not in the original field list, but this is where prep instructions live
    # for a fifth of products. Cheap to keep, expensive to discover missing.
    "cooking guidelines": ("cooking_guidelines", "text"),
    "brand": ("brand", "text"),
    "manufacturer": ("manufacturer", "text"),
    "alcohol by volume": ("alcohol_by_volume", "text"),
}

NOISE_TAGS = ("header", "nav", "footer", "script", "style", "noscript")
HEADING = re.compile(r"^h([1-6])$")
BLOCK_TAGS = {"p", "div", "li", "br", "tr", "section", "article", "ul", "ol",
              "table", "h1", "h2", "h3", "h4", "h5", "h6"}

# Deliberately broad. A signal missed here deletes an allergen silently, which
# is the worst defect this data can carry.
EMPHASIS_TAGS = {"b", "strong", "em", "u", "mark"}
EMPHASIS_CLASS = re.compile(r"allerg|bold|emphas|strong", re.I)
EMPHASIS_STYLE = re.compile(r"font-weight\s*:\s*(bold|[6-9]\d\d)", re.I)

# Some suppliers mark allergens by capitalising them instead of bolding —
# "Whole MILK Powder" — which the regulation allows and which carries no markup
# at all. Without this the allergen is dropped in silence.
CAPS_TOKEN = re.compile(r"\b[A-Z][A-Z'’]{2,}\b")
CAPS_STOPWORDS = {"AND", "THE", "FOR", "MAY", "CONTAINS", "CONTAIN", "TRACES",
                  "FREE", "FROM", "WITH", "NON", "GMO", "RSPO", "UHT", "EU",
                  "UK", "MSC", "ASC", "BPA", "VAT",
                  # Labels and certifications, not ingredients.
                  "INGREDIENTS", "INGREDIENT", "IGP", "DOP", "PDO", "PGI",
                  # Additives that are always written in caps.
                  "EDTA", "MSG", "DHA", "EPA", "BHA", "BHT"}
# When the whole field is upper case, capitalisation distinguishes nothing.
CAPS_MAX_SHARE = 0.6

NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*([a-zA-Zµ%]+)?")

# Product images are served as /images-v3/{tenant}/{image}/{size}.{ext}. The
# tenant uuid is constant across the site, the second identifies the image, and
# the last segment is a rendition — every size exists for every image, so the
# largest can be requested directly. Verified: the constructed 1280 webp returns
# 200 image/webp.
IMAGE_URL = re.compile(
    r"https://www\.ocado\.com/images-v3/([0-9a-f-]{36})/([0-9a-f-]{36})/\d+x\d+\.\w+")
IMAGE_RENDITION = "1280x1280.webp"


def strip_noise(soup: BeautifulSoup) -> None:
    """Ocado's footer carries its own h2s, which would anchor phantom sections."""
    for tag in soup(list(NOISE_TAGS)):
        tag.decompose()
    # Accessibility labels and icon text would otherwise land inside bodies.
    for tag in soup.select('[aria-hidden="true"]'):
        tag.decompose()


def section_strings(heading: Tag) -> list[NavigableString]:
    """Collect a section body by reading order, not by sibling relationship.

    With markup like <div><h2>Title</h2></div><div>body</div> the heading has no
    useful sibling, so we walk forward through the document and stop at the next
    heading of equal or higher rank — an h3 inside an h2 section is a subsection
    and stays.

    Only text nodes are returned, never elements. Returning elements looks
    tempting but is wrong: with <h2>Title</h2><div><p>body</p><h2>Next</h2>…</div>
    the outermost node inside the section is a div that also contains every
    later section, so rendering it spills the rest of the page into this field.
    Text nodes cannot over-reach, because each one is individually inside the
    boundary or past it.
    """
    start = int(HEADING.match(heading.name).group(1))
    # The heading's own text is technically "after" the heading. Without this it
    # gets glued to the front of the body.
    own = {id(d) for d in heading.descendants}

    out: list = []
    for node in heading.next_elements:
        if id(node) in own:
            continue
        if isinstance(node, Tag):
            m = HEADING.match(node.name or "")
            if m and int(m.group(1)) <= start:
                break
            # <br> separates siblings rather than containing them, so it leaves
            # no trace in a text node's ancestry. Carried along explicitly or a
            # <br>-delimited nutrition panel collapses into a single line.
            if node.name == "br":
                out.append(node)
        elif isinstance(node, NavigableString) and str(node).strip():
            out.append(node)
    return out


def is_break(node) -> bool:
    return isinstance(node, Tag) and node.name == "br"


def _block_id(node: NavigableString) -> int | None:
    """Identity of the nearest block-level ancestor, for line breaking."""
    for parent in node.parents:
        if isinstance(parent, Tag) and parent.name in BLOCK_TAGS:
            return id(parent)
    return None


def _ancestor(node: NavigableString, name: str, depth: int = 8) -> Tag | None:
    for i, parent in enumerate(node.parents):
        if i >= depth:
            break
        if isinstance(parent, Tag) and parent.name == name:
            return parent
    return None


def text_of(strings: list[NavigableString]) -> str:
    """Flatten to text, breaking a line whenever the block container changes.

    Without this a multi-step cooking instruction collapses into one run-on
    paragraph (section 8.4).
    """
    parts: list[str] = []
    previous: object = object()
    forced = False
    for node in strings:
        if is_break(node):
            forced = True
            continue
        text = str(node).strip()
        if not text:
            continue
        block = _block_id(node)
        parts.append("\n" if parts and (forced or block != previous) else " ")
        parts.append(text)
        previous = block
        forced = False

    joined = "".join(parts)
    joined = re.sub(r"[ \t]{2,}", " ", joined)
    return re.sub(r"\n{2,}", "\n", joined).strip()


def list_of(strings: list[NavigableString]) -> list[str]:
    """Group by list item where there is one, otherwise fall back to lines."""
    items: dict[int, list[str]] = {}
    order: list[int] = []
    loose: list[NavigableString] = []

    for node in strings:
        if is_break(node):
            continue
        li = _ancestor(node, "li")
        if li is None:
            loose.append(node)
            continue
        key = id(li)
        if key not in items:
            items[key] = []
            order.append(key)
        items[key].append(str(node).strip())

    if order:
        return [" ".join(p for p in items[k] if p).strip() for k in order]
    return [line for line in text_of(loose).split("\n") if line.strip()]


def is_emphasised(tag: Tag) -> bool:
    if tag.name in EMPHASIS_TAGS:
        return True
    if EMPHASIS_STYLE.search(tag.get("style", "") or ""):
        return True
    return bool(EMPHASIS_CLASS.search(" ".join(tag.get("class", []) or [])))


def capitalised_tokens(text: str) -> list[str]:
    """Find allergens marked by capitalisation rather than by markup.

    Only meaningful against mixed-case surroundings: 'E621 MONOSODIUM
    L-GLUTAMATE' is entirely upper case, so nothing there is being singled out.
    """
    words = re.findall(r"[A-Za-z][A-Za-z'’]{2,}", text)
    if not words:
        return []
    upper = [w for w in words if w.isupper()]
    if len(upper) / len(words) > CAPS_MAX_SHARE:
        return []
    return [t for t in CAPS_TOKEN.findall(text) if t not in CAPS_STOPWORDS]


def rich_of(strings: list[NavigableString]) -> dict:
    """Three views of a field whose emphasis carries regulatory meaning.

    UK retained EU 1169/2011 Annex II requires allergens to be typographically
    distinguished from the rest of the ingredient list. Ocado does that with
    inline <b>/<strong>, so get_text() on this field would discard precisely the
    information worth having.
    """
    plain: list[str] = []
    md: list[str] = []
    emphasised: list[str] = []
    previous: object = object()

    forced = False
    for node in strings:
        if is_break(node):
            forced = True
            continue
        text = str(node).strip()
        if not text:
            continue

        emph = False
        for i, parent in enumerate(node.parents):
            if i >= 6 or not isinstance(parent, Tag):
                break
            if is_emphasised(parent):
                emph = True
                break

        separator = "\n" if plain and (forced or _block_id(node) != previous) else " "
        plain.append(separator)
        md.append(separator)
        plain.append(text)
        md.append(f"**{text}**" if emph else text)
        if emph:
            emphasised.append(text)
        previous = _block_id(node)
        forced = False

    def tidy(parts: list[str]) -> str:
        text = "".join(parts)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return re.sub(r"\n{2,}", "\n", text).strip()

    plain_text = tidy(plain)
    markdown_text = tidy(md)
    signal = "markup" if emphasised else ""

    if not emphasised:
        emphasised = capitalised_tokens(plain_text)
        if emphasised:
            signal = "capitalisation"
            for token in dict.fromkeys(emphasised):
                markdown_text = re.sub(rf"\b{re.escape(token)}\b",
                                       f"**{token}**", markdown_text)

    seen, unique = set(), []
    for token in emphasised:
        key = token.lower().strip(" ,.()")
        if key and key not in seen:
            seen.add(key)
            unique.append(token.strip(" ,.()"))

    return {"text": plain_text, "markdown": markdown_text,
            "emphasised": unique, "emphasis_signal": signal}


def parse_number(cell: str) -> list[dict]:
    """'1106kJ/263kcal' is two measurements, not one string."""
    out = []
    for value, unit in NUM.findall(cell or ""):
        try:
            out.append({"value": float(value.replace(",", ".")), "unit": unit or None})
        except ValueError:
            continue
    return out


def table_from_text(text: str) -> dict | None:
    """Recover a nutrition panel that was typed out rather than marked up.

    Shape is a column caption followed by one 'label value' pair per line:

        Per 100g / Per 100ml:
        Energy Kj   2881
        Fat g       63g
    """
    lines = [line.strip() for line in (text or "").split("\n") if line.strip()]
    if not lines:
        return None

    column, rows, notes = "", [], []
    pair = re.compile(r"^(?P<label>.*?[A-Za-z)])[\s:]+(?P<value>[\d.,]+\s*[a-zA-Zµ%]*)$")

    for line in lines:
        if not column and re.search(r"\bper\b", line, re.I) and not pair.match(line):
            column = line.rstrip(":").strip()
            continue
        m = pair.match(line)
        if m:
            label = m.group("label").strip()
            value = m.group("value").strip()
            key = column or "value"
            rows.append({"label": label, "values": {key: value},
                         "parsed": {key: parse_number(value)}})
        else:
            notes.append(line)

    if not rows:
        return None
    return {"columns": [column or "value"], "rows": rows, "notes": notes,
            "source": "text"}


def table_of(strings: list[NavigableString]) -> dict | None:
    """Columns are data, not schema (section 8.6).

    Per 100g, per 1/2 pizza and per 30g serving are all legitimate; flattening
    them into fixed columns puts one product's values under another's header.
    """
    table = None
    for node in strings:
        table = _ancestor(node, "table", depth=12)
        if table is not None:
            break
    if table is None:
        # Some suppliers type the panel out as <br>-separated lines instead of
        # marking up a table. Same information, no <table> to find.
        return table_from_text(text_of(strings))

    rows = table.find_all("tr")
    if not rows:
        return None

    header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    columns = [c for c in header[1:] if c]

    body, notes = [], []
    for tr in rows[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        cells = [c for c in cells if c is not None]
        if not cells:
            continue
        # A row with one populated cell is a footnote, not a nutrient.
        if len([c for c in cells if c]) == 1:
            notes.append(next(c for c in cells if c))
            continue
        label, values = cells[0], cells[1:]
        mapped = {columns[i]: v for i, v in enumerate(values) if i < len(columns)}

        # Ocado splits Energy over two rows — kJ then kcal — and the second row
        # has no label. Left alone it becomes a nutrient called "".
        if not label and body:
            for column, value in mapped.items():
                if not value:
                    continue
                body[-1]["values"][column] = f"{body[-1]['values'].get(column, '')} {value}".strip()
                body[-1]["parsed"][column] = parse_number(body[-1]["values"][column])
            continue

        body.append({
            "label": label,
            "values": mapped,
            "parsed": {k: parse_number(v) for k, v in mapped.items()},
        })

    return {"columns": columns, "rows": body, "notes": notes}


def structured_data(soup: BeautifulSoup) -> dict:
    """Read the schema.org Product block.

    This is the page stating its own identity — name, sku, brand and crucially
    `size`, which is the pack size and appears nowhere in the h1. Identity is
    settled against this rather than against parsed prose.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or item.get("@type") != "Product":
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            brand = item.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            return {
                "name": item.get("name", ""),
                "sku": str(item.get("sku", "")),
                "brand": brand or "",
                "size": item.get("size", ""),
                "gtin": str(item.get("gtin13") or item.get("gtin") or ""),
                "image": item.get("image") or [],
                "price": offers.get("price", ""),
                "currency": offers.get("priceCurrency", ""),
                "availability": str(offers.get("availability", "")).rsplit("/", 1)[-1],
            }
    return {}


def image_urls(html: str, structured: dict) -> tuple[str, list[str]]:
    """Collect product images at the largest rendition.

    The primary comes from the JSON-LD `image` array, which is the page naming
    its own main image rather than us guessing from document order. The rest of
    the gallery is picked up from the srcset attributes.
    """
    gallery: list[str] = []
    seen: set[str] = set()

    def add(tenant: str, image: str) -> str:
        url = f"https://www.ocado.com/images-v3/{tenant}/{image}/{IMAGE_RENDITION}"
        if image not in seen:
            seen.add(image)
            gallery.append(url)
        return url

    primary = ""
    declared = structured.get("image") or []
    for url in [declared] if isinstance(declared, str) else declared:
        m = IMAGE_URL.match(str(url))
        if m:
            primary = add(m.group(1), m.group(2))
            break

    for tenant, image in IMAGE_URL.findall(html):
        add(tenant, image)

    if not primary and gallery:
        primary = gallery[0]
    # Hand back the main image on its own and the rest as extras, so a consumer
    # never has to know that the first element was special.
    return primary, [url for url in gallery if url != primary]


def parse_page(html: str, product_id: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    # Read the JSON-LD before stripping noise — it lives in a <script>.
    structured = structured_data(soup)
    main_image, other_images = image_urls(html, structured)
    strip_noise(soup)
    h1 = soup.find("h1")
    record: dict = {
        "product_id": product_id,
        "ocado_title": h1.get_text(" ", strip=True) if h1 else "",
        "ocado_name": structured.get("name", ""),
        "ocado_brand": structured.get("brand", ""),
        "ocado_size": structured.get("size", ""),
        "ocado_gtin": structured.get("gtin", ""),
        "ocado_price": structured.get("price", ""),
        "ocado_availability": structured.get("availability", ""),
        "ocado_image_main": main_image,
        "ocado_images_other": other_images,
        "headings_seen": [],
    }

    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        text = " ".join(heading.get_text(" ", strip=True).split())
        key = text.lower().strip().rstrip(":")
        record["headings_seen"].append(text)
        if key not in FIELDS or FIELDS[key][0] in record:
            continue

        name, kind = FIELDS[key]
        nodes = section_strings(heading)
        if kind == "text":
            record[name] = text_of(nodes)
        elif kind == "list":
            record[name] = list_of(nodes)
        elif kind == "rich":
            record[name] = rich_of(nodes)
        elif kind == "table":
            record[name] = table_of(nodes)

    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-dir", default=Path("F:/Ocado/raw"), type=Path)
    ap.add_argument("--out", default=Path("F:/Ocado/out/products.jsonl"), type=Path)
    args = ap.parse_args()

    files = sorted(args.raw_dir.glob("*.html.gz"))
    if not files:
        print("no cached pages", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with args.out.open("w", encoding="utf-8") as fh:
        for path in files:
            product_id = path.name.replace(".html.gz", "")
            html = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
            record = parse_page(html, product_id)
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1

    print(f"parsed {written} pages -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
