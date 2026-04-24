"""FastAPI backend — serves the frontend and a /api/listings endpoint with daily cache."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from scraper import fetch_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CACHE_FILE = Path("cache/listings.json")
CACHE_TTL_HOURS = 24
_refresh_lock = asyncio.Lock()

app = FastAPI(title="Causeway Bay Properties")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/listings")
async def get_listings(
    refresh: bool = Query(False, description="Force a fresh scrape"),
    min_m: float = Query(10, description="Min price HK$M"),
    max_m: float = Query(20, description="Max price HK$M"),
    bedrooms: int | None = Query(None, description="Filter by bedrooms"),
):
    if not refresh and _cache_valid():
        data = json.loads(CACHE_FILE.read_text())
        data["from_cache"] = True
        return JSONResponse(data)

    async with _refresh_lock:
        # Re-check after acquiring lock
        if not refresh and _cache_valid():
            data = json.loads(CACHE_FILE.read_text())
            data["from_cache"] = True
            return JSONResponse(data)

        log.info("Fetching fresh listings (min=%.0fM max=%.0fM)…", min_m, max_m)
        try:
            result = await fetch_all(min_m=min_m, max_m=max_m, bedrooms=bedrooms)
        except Exception as exc:
            log.error("Scrape failed: %s", exc)
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text())
                data["from_cache"] = True
                data["stale"] = True
                return JSONResponse(data)
            return JSONResponse({"listings": [], "errors": [str(exc)], "sources": []}, status_code=503)

        data = {
            "listings": result["listings"],
            "sources": result["sources"],
            "errors": result["errors"],
            "count": len(result["listings"]),
            "fetched_at": datetime.now().isoformat(),
            "from_cache": False,
        }
        CACHE_FILE.parent.mkdir(exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        log.info("Cached %d listings from %s", data["count"], data["sources"])
        return JSONResponse(data)


@app.get("/api/status")
async def status():
    if CACHE_FILE.exists():
        data = json.loads(CACHE_FILE.read_text())
        fetched_at = data.get("fetched_at", "unknown")
        age_h = (datetime.now() - datetime.fromisoformat(fetched_at)).total_seconds() / 3600
        return {"cache": "valid" if age_h < CACHE_TTL_HOURS else "stale",
                "fetched_at": fetched_at, "age_hours": round(age_h, 1),
                "count": data.get("count", 0)}
    return {"cache": "empty"}


# ---------------------------------------------------------------------------
# Static files  (frontend)
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse("static/index.html")

app.mount("/", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_valid() -> bool:
    if not CACHE_FILE.exists():
        return False
    try:
        data = json.loads(CACHE_FILE.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        return datetime.now() - fetched_at < timedelta(hours=CACHE_TTL_HOURS)
    except Exception:
        return False


def start():
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)


if __name__ == "__main__":
    start()
