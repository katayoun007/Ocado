"""Connectivity probe: can we reach Ocado with a browser-impersonating TLS stack?"""
import sys

from curl_cffi import requests

URLS = [
    "https://www.ocado.com/robots.txt",
    "https://www.ocado.com/sitemaps/sitemap_index.xml",
    "https://www.ocado.com/products/cathedral-city-mini-mature-snack-cheeses/28516011",
]

HEADERS = {
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def main() -> int:
    with requests.Session(impersonate="chrome", headers=HEADERS, timeout=45) as s:
        for url in URLS:
            try:
                r = s.get(url)
                head = r.text[:120].replace("\n", " ")
                print(f"{r.status_code}  {len(r.content):>9,}  {url}")
                print(f"           {head}")
            except Exception as exc:  # noqa: BLE001 - probe wants every failure mode
                print(f"ERR  {url}  {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
