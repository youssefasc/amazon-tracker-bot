import re
import asyncio
from playwright.async_api import async_playwright
from config import AFFILIATE_TAG


def make_affiliate_url(asin: str) -> str:
    return f"https://www.amazon.eg/dp/{asin}?tag={AFFILIATE_TAG}"


def extract_asin(url: str) -> str | None:
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"asin=([A-Z0-9]{10})",
        r"/d/([A-Z0-9]{10})",
        r"/([A-Z0-9]{10})(?:[/?#]|$)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            candidate = m.group(1)
            if candidate.startswith("B") or candidate.isdigit():
                return candidate
    return None


async def _open_and_read(url: str) -> dict | None:
    """EXACT same logic as the working /debug command"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ar-EG",
            extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            print(f"HTTP: {resp.status if resp else 'None'}, URL: {page.url[:60]}")

            # Handle bot check first (same as debug)
            try:
                btn = await page.query_selector("input[type='submit'], button[type='submit'], .a-button-input")
                if btn:
                    print("Clicking bot check button")
                    await btn.click()
                    await asyncio.sleep(3)
            except:
                pass

            # Wait for redirect (same as debug)
            for _ in range(10):
                await asyncio.sleep(1)
                if "amazon.eg" in page.url and "/dp/" in page.url:
                    break

            print(f"Final URL: {page.url[:70]}")
            asin = extract_asin(page.url)

            # Read title (same as debug — wait_for_selector per selector)
            title = None
            for sel in ["#productTitle", "span#productTitle"]:
                try:
                    await page.wait_for_selector(sel, timeout=5000)
                    el = await page.query_selector(sel)
                    if el:
                        t = (await el.inner_text()).strip()
                        if t:
                            title = t
                            break
                except:
                    pass

            # Read price (same as debug)
            price = None
            for sel in [
                "span.priceToPay span.a-price-whole",
                ".apexPriceToPay span.a-price-whole",
                "span.a-price-whole",
                "#corePrice_feature_div span.a-price-whole",
                "#priceblock_ourprice",
                ".a-price .a-offscreen",
            ]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        raw = (await el.inner_text()).strip()
                        cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
                        if cleaned:
                            price = float(cleaned)
                            break
                except:
                    pass

            # Read image
            image_url = ""
            for sel in ["#landingImage", "#imgBlkFront"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        img = await el.get_attribute("src") or await el.get_attribute("data-old-hires")
                        if img:
                            image_url = img
                            break
                except:
                    pass

            print(f"Result — asin: {asin}, title: {bool(title)}, price: {price}")
            return {"asin": asin, "title": title, "price": price, "image_url": image_url}
        finally:
            await browser.close()


async def scrape_amazon_product(url: str) -> dict | None:
    try:
        data = await _open_and_read(url)
        if not data or not data["asin"] or not data["title"] or not data["price"]:
            return None
        asin = data["asin"]
        return {
            "asin": asin,
            "title": data["title"][:200],
            "url": f"https://www.amazon.eg/dp/{asin}",
            "affiliate_url": make_affiliate_url(asin),
            "image_url": data["image_url"],
            "price": data["price"],
        }
    except Exception as e:
        print(f"Scrape error: {e}")
        return None


async def get_current_price(asin: str) -> float | None:
    try:
        data = await _open_and_read(f"https://www.amazon.eg/dp/{asin}")
        return data["price"] if data else None
    except Exception as e:
        print(f"Price error: {e}")
        return None


async def search_amazon(query: str) -> list[dict]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ar-EG",
            )
            page = await context.new_page()
            await page.goto(f"https://www.amazon.eg/s?k={query.replace(' ', '+')}",
                            wait_until="domcontentloaded", timeout=30000)
            try:
                btn = await page.query_selector("input[type='submit'], .a-button-input")
                if btn:
                    await btn.click()
                    await asyncio.sleep(3)
            except:
                pass
            await asyncio.sleep(2)

            results = []
            items = await page.query_selector_all("[data-component-type='s-search-result']")
            for item in items[:5]:
                try:
                    asin = await item.get_attribute("data-asin")
                    if not asin:
                        continue
                    title_el = await item.query_selector("h2 a span")
                    title = (await title_el.inner_text()).strip() if title_el else "—"
                    price_el = await item.query_selector("span.a-price-whole")
                    price_raw = (await price_el.inner_text()).strip() if price_el else ""
                    price_clean = re.sub(r"[^\d.]", "", price_raw.replace(",", ""))
                    price = float(price_clean) if price_clean else None
                    if title and price:
                        results.append({
                            "asin": asin, "title": title[:150], "price": price,
                            "url": f"https://www.amazon.eg/dp/{asin}",
                            "affiliate_url": make_affiliate_url(asin),
                        })
                except:
                    continue
            await browser.close()
            return results
    except Exception as e:
        print(f"Search error: {e}")
        return []
