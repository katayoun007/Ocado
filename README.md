# Ocado content extraction

Finds the Ocado listing for each LemonSalt product and extracts ten content
fields from it — product information, features, country of origin, nutritional
data, storage, preparation and usage, package type, ingredients, allergen
information and dietary information.

Four independent phases, each writing to disk and separately re-runnable. The
split exists so that rewriting the parser costs zero requests.

```
sitemap ──▶ [1] catalogue.parquet
                      │
input CSV ──▶ [2] matching ──┴──▶ candidates.csv
                      │
              [3] fetch ─────────▶ raw/{id}.html.gz
                      │
              [4] parse ─────────▶ products.jsonl
                      │
              [4b] verify ───────▶ verified.csv
                      │
              [5] export ────────▶ ocado_content.{jsonl,csv}
```

## Running it

```bash
python scripts/prepare_input.py --report "path/to/Product report.csv"
python scripts/build_catalogue.py --probe-part2
python scripts/match.py --input data/input_consumable_instock.csv
python scripts/fetch.py
python scripts/parse.py
python scripts/verify.py
python scripts/export.py
```

`fetch.py` skips anything already in `raw/`, so an interrupted run resumes for
free and a re-parse costs nothing.

## What was learned in the process

Findings that contradicted or extended the original plan.

**The bot challenge is geographic.** Ocado sits behind AWS WAF. From outside the
UK every URL — including `robots.txt` — returns HTTP 202 with a JavaScript
challenge, and TLS impersonation does not help because the obstacle is not a TLS
fingerprint. From a UK IP the site serves normally. There is still a volume
ceiling: the challenge resumed after roughly 830 pages in one run.

**Slugs cannot establish product identity.** 1,897 slugs cover 4,194 catalogue
entries, so genuinely different products share a slug. Only 3.2% of slugs carry
a pack size, and most of those are not pack sizes at all — `9-14kg` is a nappy's
weight range. Identity is therefore settled against the page, not the slug.

**The page states its own identity.** Every product page carries a schema.org
JSON-LD block with `name`, `sku`, `brand` and `size`. `size` is the pack size and
appears nowhere in the h1. This is what `verify.py` compares against.

**Allergens are not always marked with markup.** The plan expected `<b>`/
`<strong>`. Some suppliers capitalise instead — `Whole MILK Powder` — which the
regulation permits and which carries no markup at all. Detecting only markup
dropped the allergens of eleven products in silence. `parse.py` treats
capitalisation as a fourth emphasis signal, but only when the surrounding text
is mixed case, since `E621 MONOSODIUM L-GLUTAMATE` singles out nothing.

**Brand decides, and a score cannot outvote it.** The worst failures all looked
like good matches: "Bodrum Red Split Lentils" against Ocado's own red split
lentils, "TRS All Purpose Seasoning" against M&S's, "Saloio Extra Virgin Olive
Oil" against El Molino's. Five of six words agree, so `token_set_ratio` scores
them in the high eighties. Calibrating on the matches already believed settled
the question: of 119 confirmed, only two disagreed on the brand the page states,
and both of those two were wrong. So brand is now checked before any score is
read, and a candidate that fails it is out. It moved 751 products out of the
answer and three wrong ones out of an earlier export.

Three things had to be right for that rule to work. A brand word shared by
enough distinct brands identifies none of them — "Ahmad Tea" was confirmed
against "Brew Tea Co" on the strength of "tea" — so the generic ones are counted
out of the 712 brands the pages state rather than listed by hand. "Lingham's" and
"Linghams" have to compare equal, which means comparing the separator-free forms,
and that test needs a length floor because "M&S" reduces to "ms", which is inside
"bodrumsunfloweroil". And Ocado sometimes credits the distributor, leaving the
real brand in the name — "Brindisa Ortiz Yellowfin Tuna" — so the input's own
leading word appearing in the page name counts as agreement too.

**A token scorer needs tokens.** `token_set_ratio` was being handed slug form,
and rapidfuzz splits on whitespace, so `serious-pig-snacking-pickles` arrived as
a single token and the scorer silently degraded to character similarity. That
pair scored 66.7 against `serious-pig-snacking-pickles-crunchy-tangy-mini-gherkins`
— the same product, one name simply fuller — where a real token comparison gives
100. Everything downstream was calibrated on a number that was not measuring what
it claimed. Passing space-separated form recovered 47 products, and it made the
wrong matches score *lower*, not higher: "Salsa Mexicana Casera" against "Salsa
Ranchera" fell from 80.7 to 78.

**Which side the extra words are on decides it.** Rarity looked like the way to
tell "Salsa Mexicana Casera" from "Achiote Seasoning Paste", and it is not: the
words that actually fork a product line are usually ordinary ones — "Fig, Apple &
Garlic Chutney" against "Peach & Mango Chutney", "Mushroom Soy Sauce" against
"Fish Sauce" — while genuinely identical pairs were separated by rare ones, a
parent brand ("Nestle Carnation" / "Carnation") or a spelling ("Lemonade" /
"Limonade"). The shape of the disagreement separates them where rarity cannot.
Both sides carrying a word the other lacks is a fork. Only Ocado carrying extra
words is elaboration. Only the input carrying an extra word is a fork again if
that word is distinctive, because it may be the product — "Rummo Mezze Penne
Rigate" is a different shape from "Rummo Penne Rigate No. 66". None of it
rejects: "Rio Mare Insalatissime Mexican Style Tuna Salad" and Ocado's "Rio Mare
MSC Tuna Salad Mexican Style" diverge both ways and are the same tin, so
divergence withholds `confirmed` and asks for a human.

**The matching stage should not reject.** Slug similarity cannot distinguish a
real match from a miss: "Regal Original Cake Rusks" scored 59.3 against Ocado's
`regal-original-cake-rusk`, the same product. The gate is now set for recall and
precision is bought later in `verify.py`, on evidence. Volume is held down by
the brand-absence rule — if a brand's leading token appears in none of the
49,735 slugs, Ocado does not stock the brand — which is a fact about the
catalogue rather than a guess about a score.

## Layout

| Path | Contents |
|---|---|
| `scripts/` | The pipeline |
| `data/` | Inputs, match candidates, review queues, stats |
| `out/ocado_content.jsonl` | Deliverable, nested and lossless |
| `out/ocado_content.csv` | Deliverable, flat |
| `out/quality_report.json` | Per-field coverage and quality flags |
| `raw/` | Page cache, not committed |

## Quality checks

`export.py` runs the checks that catch silent failures:

- an ingredient list that names a declarable allergen with nothing emphasised —
  the signal that the markup changed. Restricted to lists that actually name one:
  checking every list flagged 112 products, almost all of them strawberry jam and
  sea salt with no allergen to mark, which buried the ten real cases
- emphasised allergens absent from the Allergen Information text
- more than 100g of a nutrient per 100g, meaning a column was misread
- input name and Ocado name disagreeing, meaning the match was wrong

The coverage rule from the plan is what proves the parser is not at fault: for
every field, the number of pages where the heading is present but the value came
out empty is zero. Empty means Ocado did not publish the section.
