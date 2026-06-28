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


REDIRECT_DOMAINS = ["amzn.to", "amzn.eu", "link.amazon.com", "link.amazon/", "amzn.com", "a.co"]
AMAZON_PRODUCT_DOMAINS = ["amazon.eg", "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr"]


async def extract_product_from_page(page) -> dict | None:
    """Extract product info from an already-loaded Amazon page"""
    # Wait for product title to appear
    try:
        await page.wait_for_selector("#productTitle", timeout=10000)
    except:
        pass

    await asyncio.sleep(1)

    # Title
    title = None
    for selector in ["#productTitle", "span#productTitle", "h1.a-size-large"]:
        try:
            el = await page.query_selector(selector)
            if el:
                title = (await el.inner_text()).strip()
                if title:
                    break
        except:
            pass

    # Price
    price = None
    price_selectors = [
        "span.priceToPay span.a-price-whole",
        ".apexPriceToPay span.a-price-whole",
        "#corePrice_feature_div span.a-price-whole",
        "span.a-price-whole",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
        ".a-price .a-offscreen",
    ]
    for selector in price_selectors:
        try:
            el = await page.query_selector(selector)
            if el:
                raw = (await el.inner_text()).strip()
                cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
                if cleaned:
                    price = float(cleaned)
                    break
        except:
            pass

    # Image
    image_url = None
    for selector in ["#landingImage", "#imgBlkFront", "#main-image"]:
        try:
            el = await page.query_selector(selector)
            if el:
                image_url = await el.get_attribute("src")
                if not image_url:
                    image_url = await el.get_attribute("data-old-hires")
                if image_url:
                    break
        except:
            pass

    print(f"Extracted — title: {bool(title)}, price: {price}, url: {page.url[:60]}")
    return {"title": title, "price": price, "image_url": image_url or ""}


async def scrape_amazon_product(url: str) -> dict | None:
    """Scrape product info — handles short links and direct amazon.eg links"""
    try:
        needs_resolve = any(d in url for d in REDIRECT_DOMAINS)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ar-EG",
                extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()

            if needs_resolve:
                # Open short link and wait for redirect to amazon product page
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                for _ in range(10):
                    await asyncio.sleep(1)
                    current = page.url
                    if any(d in current for d in AMAZON_PRODUCT_DOMAINS) and "/dp/" in current:
                        print(f"Redirected to: {current}")
                        break
                final_url = page.url
            else:
                # Direct amazon link
                asin = extract_asin(url)
                if not asin:
                    await browser.close()
                    return None
                final_url = f"https://www.amazon.eg/dp/{asin}"
                await page.goto(final_url, wait_until="domcontentloaded", timeout=30000)

            # Extract ASIN from final URL
            asin = extract_asin(page.url)
            if not asin:
                print(f"No ASIN in final URL: {page.url}")
                await browser.close()
                return None

            # Extract product data from the current page
            data = await extract_product_from_page(page)
            await browser.close()

            if not data["title"] or not data["price"]:
                return None

            affiliate_url = make_affiliate_url(asin)
            return {
                "asin": asin,
                "title": data["title"][:200],
                "url": f"https://www.amazon.eg/dp/{asin}",
                "affiliate_url": affiliate_url,
                "image_url": data["image_url"],
                "price": data["price"],
            }

    except Exception as e:
        print(f"Scrape error: {e}")
        return None


async def get_current_price(asin: str) -> float | None:
    """Get only the current price for a product"""
    try:
        url = f"https://www.amazon.eg/dp/{asin}"
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            try:
                await page.wait_for_selector("span.a-price-whole", timeout=8000)
            except:
                pass

            price_selectors = [
                "span.priceToPay span.a-price-whole",
                ".apexPriceToPay span.a-price-whole",
                "#corePrice_feature_div span.a-price-whole",
                "span.a-price-whole",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                ".a-price .a-offscreen",
            ]
            for selector in price_selectors:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        raw = (await el.inner_text()).strip()
                        cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
                        if cleaned:
                            await browser.close()
                            return float(cleaned)
                except:
                    pass

            await browser.close()
            return None
    except Exception as e:
        print(f"Price check error: {e}")
        return None


async def search_amazon(query: str) -> list[dict]:
    """Search Amazon Egypt for products"""
    try:
        search_url = f"https://www.amazon.eg/s?k={query.replace(' ', '+')}"
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
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
                            "asin": asin,
                            "title": title[:150],
                            "price": price,
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
