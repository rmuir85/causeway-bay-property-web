"""
Property scrapers for Causeway Bay, HK$10M–20M.

Sources:
  - 28hse.com   : POST dosearch API (no browser needed)
  - Spacious.hk : Playwright headless browser
  - Midland.com.hk : Playwright headless browser
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_MIN_M = 10   # HK$10M
DEFAULT_MAX_M = 20   # HK$20M

# 28hse district IDs  23=Causeway Bay  22=Happy Valley
DISTRICT_IDS = ["23"]


async def fetch_all(
    min_m: float = DEFAULT_MIN_M,
    max_m: float = DEFAULT_MAX_M,
    bedrooms: int | None = None,
    page: int = 1,
    use_playwright: bool = True,
) -> dict:
    """Fetch listings from all sources, return merged + deduplicated list."""
    tasks: list[asyncio.Task] = []

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        t28 = asyncio.create_task(
            fetch_28hse(client, min_m, max_m, DISTRICT_IDS, bedrooms, page)
        )
        tasks.append(t28)

        if use_playwright:
            t_sp = asyncio.create_task(fetch_spacious_playwright(min_m, max_m, bedrooms))
            t_ml = asyncio.create_task(fetch_midland_playwright(min_m, max_m, bedrooms))
            tasks.append(t_sp)
            tasks.append(t_ml)

        results = await asyncio.gather(*tasks, return_exceptions=True)

    listings: list[dict] = []
    sources_ok: list[str] = []
    errors: list[str] = []

    for result in results:
        if isinstance(result, Exception):
            errors.append(str(result))
        elif isinstance(result, tuple):
            items, source_name = result
            listings.extend(items)
            if items:
                sources_ok.append(source_name)
        elif isinstance(result, list):
            listings.extend(result)

    # Deduplicate by URL then by (name, price) fallback
    seen: set[str] = set()
    unique: list[dict] = []
    for item in listings:
        key = item.get("url") or f"{item.get('name','')}-{item.get('price','')}"
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    # Sort by price ascending
    def price_sort_key(p: dict) -> float:
        m = re.search(r"([\d.]+)\s*[Mm]illion", p.get("price", ""))
        if m:
            return float(m.group(1))
        m2 = re.search(r"HK\$?([\d.]+)", p.get("price", ""))
        if m2:
            return float(m2.group(1))
        return 999

    unique.sort(key=price_sort_key)
    return {"listings": unique, "errors": errors, "sources": sources_ok}


# ---------------------------------------------------------------------------
# 28hse  (no Playwright needed)
# ---------------------------------------------------------------------------

async def fetch_28hse(
    client: httpx.AsyncClient,
    min_m: float,
    max_m: float,
    district_ids: list[str],
    bedrooms: int | None,
    page: int,
) -> tuple[list[dict], str]:
    api_headers = {
        **HEADERS,
        "Referer": "https://www.28hse.com/en/buy",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload: list[tuple[str, str]] = [
        ("buyRent", "buy"),
        ("page", str(page)),
        ("price_low", str(int(min_m))),
        ("price_high", str(int(max_m))),
        ("lang", "en"),
    ]
    for did in district_ids:
        payload.append(("district_ids[]", did))
    if bedrooms:
        payload.append(("noOfRoom[]", str(bedrooms)))

    resp = await client.post(
        "https://www.28hse.com/en/property/dosearch",
        content=urllib.parse.urlencode(payload),
        headers=api_headers,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("status") != 1:
        raise RuntimeError(f"28hse: {result.get('msg', 'error')}")

    html = result["data"]["results"]["resultContentHtml"]
    soup = BeautifulSoup(html, "lxml")
    listings = [_parse_28hse_card(card) for card in soup.select(".property_item")]
    return [l for l in listings if l], "28hse.com"


def _parse_28hse_card(card: Tag) -> dict | None:
    prop: dict[str, str] = {"source": "28hse.com", "source_color": "#e05a2b"}

    price_el = card.select_one(".ui.right.floated.red.large.label, .ui.red.label")
    if price_el:
        prop["price"] = price_el.get_text(strip=True)

    district_el = card.select_one(".district_area")
    if district_el:
        links = district_el.find_all("a")
        if len(links) >= 2:
            prop["district"] = links[0].get_text(strip=True)
            prop["name"] = links[1].get_text(strip=True)
        elif links:
            prop["name"] = links[0].get_text(strip=True)
        unit_el = district_el.select_one(".unit_desc")
        if unit_el:
            prop["floor"] = unit_el.get_text(strip=True)

    area_el = card.select_one(".areaUnitPrice")
    if area_el:
        area_text = area_el.get_text(" ", strip=True)
        m_s = re.search(r"Saleable Area[:\s]*([\d,]+\s*ft[²2]?)", area_text)
        if m_s:
            prop["area_saleable"] = m_s.group(1).strip()
        m_g = re.search(r"Gross Area[:\s]*([\d,]+\s*ft[²2]?)", area_text)
        if m_g:
            prop["area_gross"] = m_g.group(1).strip()
        m_p = re.search(r"@\s*([\d,]+)", area_text)
        if m_p:
            prop["price_per_sqft"] = f"HK${m_p.group(1)}/ft²"

    tags_el = card.select_one(".tagLabels")
    if tags_el:
        tag_texts = [t.get_text(strip=True) for t in tags_el.select(".ui.label")]
        if tag_texts:
            prop["bedrooms"] = tag_texts[0]
            if len(tag_texts) > 1:
                prop["features"] = ", ".join(tag_texts[1:3])

    link = card.select_one("a.detail_page")
    if link and link.get("href"):
        href = str(link["href"])
        if href.startswith("/"):
            href = "https://www.28hse.com" + href
        prop["url"] = href

    img = card.select_one("img.detail_page_img")
    if img and img.get("src"):
        prop["image"] = str(img["src"])

    return prop if (prop.get("name") or prop.get("price")) else None


# ---------------------------------------------------------------------------
# Spacious.hk  (Playwright)
# ---------------------------------------------------------------------------

async def fetch_spacious_playwright(
    min_m: float, max_m: float, bedrooms: int | None
) -> tuple[list[dict], str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], "spacious.hk (playwright not installed)"

    min_price = int(min_m * 1_000_000)
    max_price = int(max_m * 1_000_000)
    url = (
        f"https://www.spacious.hk/en/hong-kong/for-sale"
        f"?district=causeway-bay&price_min={min_price}&price_max={max_price}"
    )
    if bedrooms:
        url += f"&bedrooms={bedrooms}"

    listings: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            # Wait for listing cards to appear
            await page.wait_for_selector(
                "[class*='listing-card'], [class*='ListingCard'], [class*='property-card']",
                timeout=15_000,
            )
            content = await page.content()
        except Exception:
            content = await page.content()
        finally:
            await browser.close()

    soup = BeautifulSoup(content, "lxml")

    # Try structured card selectors
    cards = soup.select(
        "[class*='listing-card'], [class*='ListingCard'], "
        "[class*='property-card'], [class*='PropertyCard']"
    )
    for card in cards[:30]:
        prop = _parse_spacious_card(card)
        if prop:
            listings.append(prop)

    # Fallback: JSON-LD
    if not listings:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                import json
                d = json.loads(s.string or "")
                items = d.get("@graph", [d]) if isinstance(d, dict) else d
                for item in items:
                    if item.get("@type") in (
                        "RealEstateListing", "SingleFamilyResidence",
                        "Apartment", "House",
                    ):
                        prop = _parse_spacious_jsonld(item)
                        if prop:
                            listings.append(prop)
            except Exception:
                pass

    return listings, "spacious.hk"


def _parse_spacious_card(card: Tag) -> dict | None:
    prop: dict[str, str] = {"source": "spacious.hk", "source_color": "#00b0a0"}

    for sel in ["[class*='price']", "[class*='Price']"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if re.search(r"[\$M]|HK|million", t, re.I):
                prop["price"] = t
                break

    for sel in ["[class*='title']", "[class*='name']", "[class*='estate']", "h2", "h3"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 2:
                prop["name"] = t
                break

    for sel in ["[class*='area']", "[class*='size']", "[class*='sqft']"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if re.search(r"sq|ft|m²", t, re.I):
                prop["area_saleable"] = t
                break

    for sel in ["[class*='bed']", "[class*='room']"]:
        el = card.select_one(sel)
        if el:
            prop["bedrooms"] = el.get_text(strip=True)
            break

    link = card.select_one("a[href]")
    if link and link.get("href"):
        href = str(link["href"])
        if href.startswith("/"):
            href = "https://www.spacious.hk" + href
        prop["url"] = href

    return prop if (prop.get("name") or prop.get("price")) else None


def _parse_spacious_jsonld(item: dict) -> dict | None:
    name = item.get("name", "")
    if not name:
        return None
    prop: dict[str, str] = {"source": "spacious.hk", "source_color": "#00b0a0"}
    prop["name"] = name

    # Parse structured name: "3 Beds, HK$13M, For Sale, ..."
    m_price = re.search(r"HK\$([\d.]+)M", name)
    if m_price:
        prop["price"] = f"HK${m_price.group(1)} Millions"
    m_beds = re.search(r"(\d+)\s*Bed", name, re.I)
    if m_beds:
        prop["bedrooms"] = f"{m_beds.group(1)} Bedrooms"

    prop["address"] = item.get("address", "")
    prop["url"] = item.get("url", "")
    if item.get("photo", {}).get("contentUrl"):
        prop["image"] = item["photo"]["contentUrl"]

    return prop if prop.get("price") else None


# ---------------------------------------------------------------------------
# Midland  (Playwright)
# ---------------------------------------------------------------------------

async def fetch_midland_playwright(
    min_m: float, max_m: float, bedrooms: int | None
) -> tuple[list[dict], str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], "midland.com.hk (playwright not installed)"

    min_price = int(min_m * 1_000_000)
    max_price = int(max_m * 1_000_000)
    url = (
        f"https://www.midland.com.hk/en/list/buy/causeway-bay"
        f"?minPrice={min_price}&maxPrice={max_price}"
    )
    if bedrooms:
        url += f"&bedroom={bedrooms}"

    listings: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            await page.wait_for_selector(
                "[class*='listing'], [class*='property'], [class*='PropertyCard']",
                timeout=15_000,
            )
            content = await page.content()
        except Exception:
            content = await page.content()
        finally:
            await browser.close()

    soup = BeautifulSoup(content, "lxml")
    cards = soup.select(
        "[class*='ListingCard'], [class*='listing-card'], "
        "[class*='property-card'], [class*='PropertyCard'], "
        "[class*='listing-item']"
    )
    for card in cards[:30]:
        prop = _parse_midland_card(card)
        if prop:
            listings.append(prop)

    return listings, "midland.com.hk"


def _parse_midland_card(card: Tag) -> dict | None:
    prop: dict[str, str] = {"source": "midland.com.hk", "source_color": "#d4372a"}

    for sel in ["[class*='price']", "[class*='Price']"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if re.search(r"[\$M]|HK|百萬", t):
                prop["price"] = t
                break

    for sel in ["[class*='name']", "[class*='estate']", "[class*='title']", "h2", "h3"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 2:
                prop["name"] = t
                break

    for sel in ["[class*='area']", "[class*='size']"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if re.search(r"sq|ft|m²|呎", t, re.I):
                prop["area_saleable"] = t
                break

    link = card.select_one("a[href]")
    if link and link.get("href"):
        href = str(link["href"])
        if href.startswith("/"):
            href = "https://www.midland.com.hk" + href
        prop["url"] = href

    return prop if (prop.get("name") or prop.get("price")) else None
