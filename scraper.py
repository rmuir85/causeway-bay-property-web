"""
Property scrapers — Causeway Bay (Fashion Walk streets), HK$10M–20M, 3+ bedrooms.

Sources:
  - 28hse.com      : POST dosearch API (no browser needed)
  - Squarefoot.hk  : JSON search API (no browser needed)
  - Spacious.hk    : Playwright headless browser
  - Centaline      : Playwright headless browser
  - Ricacorp       : Playwright headless browser
  - Midland.com.hk : Playwright headless browser
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_MIN_M    = 10    # HK$10M
DEFAULT_MAX_M    = 20    # HK$20M
DEFAULT_BEDS_MIN = 3     # 3+ bedrooms

# 28hse district IDs: 23 = Causeway Bay, 22 = Happy Valley (adjacent)
DISTRICT_IDS = ["23"]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def fetch_all(
    min_m: float = DEFAULT_MIN_M,
    max_m: float = DEFAULT_MAX_M,
    beds_min: int = DEFAULT_BEDS_MIN,
    page: int = 1,
    use_playwright: bool = True,
) -> dict:
    """Fetch from all sources, return merged + deduplicated list sorted by price."""
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        tasks: list[asyncio.Task] = [
            asyncio.create_task(fetch_28hse(client, min_m, max_m, DISTRICT_IDS, beds_min, page)),
            asyncio.create_task(fetch_squarefoot(client, min_m, max_m, beds_min)),
        ]
        if use_playwright:
            tasks += [
                asyncio.create_task(fetch_spacious_playwright(min_m, max_m, beds_min)),
                asyncio.create_task(fetch_centaline_playwright(min_m, max_m, beds_min)),
                asyncio.create_task(fetch_ricacorp_playwright(min_m, max_m, beds_min)),
                asyncio.create_task(fetch_midland_playwright(min_m, max_m, beds_min)),
            ]
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

    # Deduplicate by URL then by (name, price) fallback
    seen: set[str] = set()
    unique: list[dict] = []
    for item in listings:
        key = item.get("url") or f"{item.get('name','')}-{item.get('price','')}"
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(key=_price_sort_key)
    return {"listings": unique, "errors": errors, "sources": sources_ok}


def _price_sort_key(p: dict) -> float:
    m = re.search(r"([\d.]+)\s*[Mm]illion", p.get("price", ""))
    if m:
        return float(m.group(1))
    m2 = re.search(r"HK\$?([\d.]+)", p.get("price", ""))
    if m2:
        return float(m2.group(1))
    return 999


# ---------------------------------------------------------------------------
# 28hse  (HTTP API — no browser needed)
# ---------------------------------------------------------------------------

async def fetch_28hse(
    client: httpx.AsyncClient,
    min_m: float,
    max_m: float,
    district_ids: list[str],
    beds_min: int,
    page: int,
) -> tuple[list[dict], str]:
    api_headers = {
        **HEADERS,
        "Referer": "https://www.28hse.com/en/buy",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    base_payload: list[tuple[str, str]] = [
        ("buyRent", "buy"),
        ("page", str(page)),
        ("price_low", str(int(min_m))),
        ("price_high", str(int(max_m))),
        ("lang", "en"),
    ]
    for did in district_ids:
        base_payload.append(("district_ids[]", did))

    # Fetch each bedroom count >= beds_min separately and merge
    all_listings: list[dict] = []
    seen_urls: set[str] = set()

    for beds in range(beds_min, 6):  # e.g. 3, 4, 5
        bed_payload = base_payload + [("noOfRoom[]", str(beds))]
        resp = await client.post(
            "https://www.28hse.com/en/property/dosearch",
            content=urllib.parse.urlencode(bed_payload),
            headers=api_headers,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") != 1:
            continue
        html = result["data"]["results"]["resultContentHtml"]
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select(".property_item"):
            prop = _parse_28hse_card(card)
            if prop:
                key = prop.get("url") or f"{prop.get('name','')}-{prop.get('price','')}"
                if key not in seen_urls:
                    seen_urls.add(key)
                    all_listings.append(prop)

    return all_listings, "28hse.com"


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
# Squarefoot.com.hk  (JSON API — no browser needed)
# ---------------------------------------------------------------------------

async def fetch_squarefoot(
    client: httpx.AsyncClient,
    min_m: float,
    max_m: float,
    beds_min: int,
) -> tuple[list[dict], str]:
    min_price = int(min_m * 1_000_000)
    max_price = int(max_m * 1_000_000)

    api_headers = {
        **HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.squarefoot.com.hk/en/sale/",
        "X-Requested-With": "XMLHttpRequest",
    }

    params = {
        "lang": "en",
        "sale_type": "S",
        "district": "CAUB",
        "price_from": str(min_price),
        "price_to": str(max_price),
        "bedroom_from": str(beds_min),
        "num_rec": "50",
        "page": "1",
    }

    listings: list[dict] = []

    try:
        resp = await client.get(
            "https://www.squarefoot.com.hk/api/listing/search/",
            params=params,
            headers=api_headers,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("results") or data.get("data") or data.get("listings") or []
        if isinstance(items, dict):
            items = items.get("items") or items.get("list") or []
        for item in items[:50]:
            prop = _parse_squarefoot_item(item)
            if prop:
                listings.append(prop)
    except Exception:
        listings = await _fetch_squarefoot_html(client, min_m, max_m, beds_min)

    return listings, "squarefoot.com.hk"


async def _fetch_squarefoot_html(
    client: httpx.AsyncClient,
    min_m: float,
    max_m: float,
    beds_min: int,
) -> list[dict]:
    min_price = int(min_m * 1_000_000)
    max_price = int(max_m * 1_000_000)
    url = (
        f"https://www.squarefoot.com.hk/en/sale/"
        f"?district=causeway-bay"
        f"&price_from={min_price}&price_to={max_price}"
        f"&bedroom_from={beds_min}"
    )
    listings: list[dict] = []
    try:
        resp = await client.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for card in soup.select(
            "[class*='listing-item'], [class*='ListingItem'], [class*='property-item']"
        )[:30]:
            prop = _parse_squarefoot_card(card)
            if prop:
                listings.append(prop)
    except Exception:
        pass
    return listings


def _parse_squarefoot_item(item: dict) -> dict | None:
    prop: dict[str, str] = {"source": "squarefoot.com.hk", "source_color": "#0057b7"}
    if not item:
        return None

    price_raw = item.get("price") or item.get("ask_price") or item.get("asking_price")
    if price_raw:
        try:
            p = float(str(price_raw).replace(",", ""))
            prop["price"] = f"HK${p/1_000_000:.1f} Millions"
        except ValueError:
            prop["price"] = str(price_raw)

    prop["name"] = (
        item.get("building_name") or item.get("name") or
        item.get("estate_name") or item.get("title") or ""
    )
    prop["district"] = item.get("district_name") or item.get("district") or "Causeway Bay"
    prop["floor"] = item.get("floor") or item.get("floor_desc") or ""

    area = item.get("saleable_area") or item.get("net_area") or item.get("area")
    if area:
        prop["area_saleable"] = f"{area} ft²"

    beds = item.get("bedroom") or item.get("bedrooms") or item.get("num_bedroom")
    if beds:
        prop["bedrooms"] = f"{beds} Bedrooms"

    psf = item.get("price_per_sqft") or item.get("psf")
    if psf:
        prop["price_per_sqft"] = f"HK${psf}/ft²"

    slug = item.get("slug") or item.get("listing_id") or item.get("id")
    if slug:
        prop["url"] = f"https://www.squarefoot.com.hk/en/sale/{slug}/"
    elif item.get("url"):
        prop["url"] = str(item["url"])

    img = item.get("image") or item.get("photo") or item.get("cover_image")
    if isinstance(img, dict):
        img = img.get("url") or img.get("src") or ""
    if img:
        prop["image"] = str(img)

    return prop if (prop.get("name") or prop.get("price")) else None


def _parse_squarefoot_card(card: Tag) -> dict | None:
    prop: dict[str, str] = {"source": "squarefoot.com.hk", "source_color": "#0057b7"}

    for sel in ["[class*='price']", "[class*='Price']"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if re.search(r"[\$M]|HK|million", t, re.I):
                prop["price"] = t
                break

    for sel in ["[class*='name']", "[class*='title']", "[class*='estate']", "h2", "h3"]:
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

    link = card.select_one("a[href]")
    if link and link.get("href"):
        href = str(link["href"])
        if href.startswith("/"):
            href = "https://www.squarefoot.com.hk" + href
        prop["url"] = href

    return prop if (prop.get("name") or prop.get("price")) else None


# ---------------------------------------------------------------------------
# Centaline / centanet.com  (Playwright)
# ---------------------------------------------------------------------------

async def fetch_centaline_playwright(
    min_m: float, max_m: float, beds_min: int
) -> tuple[list[dict], str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], "centaline (playwright not installed)"

    min_price = int(min_m * 1_000_000)
    max_price = int(max_m * 1_000_000)
    url = (
        f"https://hk.centanet.com/findproperty/en/list/buy"
        f"?q=causeway-bay"
        f"&minPrice={min_price}&maxPrice={max_price}"
        f"&minBedroom={beds_min}"
    )

    listings: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-US")
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            await page.wait_for_selector(
                "[class*='listing'], [class*='property'], [class*='card']",
                timeout=15_000,
            )
            content = await page.content()
        except Exception:
            content = await page.content()
        finally:
            await browser.close()

    soup = BeautifulSoup(content, "lxml")
    cards = soup.select(
        "[class*='listing-card'], [class*='ListingCard'], "
        "[class*='property-card'], [class*='PropertyCard'], "
        "[class*='listing-item'], [class*='result-item']"
    )
    for card in cards[:30]:
        prop = _parse_centaline_card(card)
        if prop:
            listings.append(prop)

    return listings, "centaline.com"


def _parse_centaline_card(card: Tag) -> dict | None:
    prop: dict[str, str] = {"source": "centaline.com", "source_color": "#c0392b"}

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

    for sel in ["[class*='area']", "[class*='size']", "[class*='sqft']"]:
        el = card.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if re.search(r"sq|ft|m²|呎", t, re.I):
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
            href = "https://hk.centanet.com" + href
        prop["url"] = href

    img = card.select_one("img[src]")
    if img and img.get("src") and "http" in str(img["src"]):
        prop["image"] = str(img["src"])

    return prop if (prop.get("name") or prop.get("price")) else None


# ---------------------------------------------------------------------------
# Ricacorp  (Playwright)
# ---------------------------------------------------------------------------

async def fetch_ricacorp_playwright(
    min_m: float, max_m: float, beds_min: int
) -> tuple[list[dict], str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], "ricacorp.com (playwright not installed)"

    min_price = int(min_m * 1_000_000)
    max_price = int(max_m * 1_000_000)
    url = (
        f"https://www.ricacorp.com/en/property/list/buy"
        f"?district=causeway-bay"
        f"&minprice={min_price}&maxprice={max_price}"
        f"&bedroom={beds_min}"
    )

    listings: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-US")
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
            await page.wait_for_selector(
                "[class*='listing'], [class*='property'], [class*='card']",
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
        prop = _parse_ricacorp_card(card)
        if prop:
            listings.append(prop)

    return listings, "ricacorp.com"


def _parse_ricacorp_card(card: Tag) -> dict | None:
    prop: dict[str, str] = {"source": "ricacorp.com", "source_color": "#1a6b3c"}

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
            href = "https://www.ricacorp.com" + href
        prop["url"] = href

    img = card.select_one("img[src]")
    if img and img.get("src") and "http" in str(img["src"]):
        prop["image"] = str(img["src"])

    return prop if (prop.get("name") or prop.get("price")) else None


# ---------------------------------------------------------------------------
# Spacious.hk  (Playwright)
# ---------------------------------------------------------------------------

async def fetch_spacious_playwright(
    min_m: float, max_m: float, beds_min: int
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
        f"&bedrooms_min={beds_min}"
    )

    listings: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-US")
        try:
            await page.goto(url, wait_until="networkidle", timeout=45_000)
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
    cards = soup.select(
        "[class*='listing-card'], [class*='ListingCard'], "
        "[class*='property-card'], [class*='PropertyCard']"
    )
    for card in cards[:30]:
        prop = _parse_spacious_card(card)
        if prop:
            listings.append(prop)

    if not listings:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                d = json.loads(s.string or "")
                items = d.get("@graph", [d]) if isinstance(d, dict) else d
                for item in items:
                    if item.get("@type") in (
                        "RealEstateListing", "SingleFamilyResidence", "Apartment", "House",
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
# Midland.com.hk  (Playwright)
# ---------------------------------------------------------------------------

async def fetch_midland_playwright(
    min_m: float, max_m: float, beds_min: int
) -> tuple[list[dict], str]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], "midland.com.hk (playwright not installed)"

    min_price = int(min_m * 1_000_000)
    max_price = int(max_m * 1_000_000)
    url = (
        f"https://www.midland.com.hk/en/list/buy/causeway-bay"
        f"?minPrice={min_price}&maxPrice={max_price}&bedroom={beds_min}"
    )

    listings: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-US")
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
