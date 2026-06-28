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


async def bypass_bot_check(page) -> bool:
    """Click continue button if Amazon shows bot check, then wait for product page"""
    try:
        for selector in [
            "input[type='submit']",
            "button[type='submit']",
            ".a-button-input",
            "[name='continue']",
        ]:
            el = await page.query_selector(selector)
            if el:
                print(f"Bot check detected — clicking {selector}")
                await el.click()
                # Wait for navigation after click
                for _ in range(10):
                    await asyncio.sleep(1)
                    if "/dp/" in page.url or "/gp/" in page.url:
                        print(f"After bypass: {page.url[:60]}")
                        return True
                return True
    except:
        pass
    return False


async def new_browser_context(p):
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"]
    )
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="ar-EG",
        extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
    )
    return browser, context


async def scrape_amazon_product(url: str) -> dict | None:
    """Scrape product — same logic as /debug command"""
    try:
        needs_resolve = any(d in url for d in REDIRECT_DOMAINS)

        async with async_playwright() as p:
            browser, context = await new_browser_context(p)
            page = await context.new_page()

            resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            print(f"HTTP: {resp.status if resp else 'None'}, URL: {page.url[:60]}")

            # Save ASIN from initial URL if present (before any bypass)
            initial_asin = extract_asin(page.url)

            # Handle bot check
            await bypass_bot_check(page)

            # If short link, wait for redirect to amazon product page
            if needs_resolve:
                for _ in range(10):
                    await asyncio.sleep(1)
                    current = page.url
                    if any(d in current for d in AMAZON_PRODUCT_DOMAINS) and "/dp/" in current:
                        print(f"Redirected to: {current[:60]}")
                        break
                # Handle bot check again after redirect
                await bypass_bot_check(page)

            print(f"Final URL: {page.url[:80]}")

            # Extract ASIN — prefer from final URL, fallback to initial
            asin = extract_asin(page.url) or initial_asin
            if not asin:
                print(f"No ASIN found in: {page.url}")
                await browser.close()
                return None

            # If not on product page, navigate directly using ASIN
            if "/dp/" not in page.url:
                product_url = f"https://www.amazon.eg/dp/{asin}"
                print(f"Navigating directly to: {product_url}")
                await page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
                await bypass_bot_check(page)

            # Wait for product title
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
            for selector in [
                "span.priceToPay span.a-price-whole",
                ".apexPriceToPay span.a-price-whole",
                "#corePrice_feature_div span.a-price-whole",
                "span.a-price-whole",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                ".a-price .a-offscreen",
            ]:
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

            await browser.close()
            print(f"Result — title: {bool(title)}, price: {price}")

            if not title or not price:
                return None

            return {
                "asin": asin,
                "title": title[:200],
                "url": f"https://www.amazon.eg/dp/{asin}",
                "affiliate_url": make_affiliate_url(asin),
                "image_url": image_url or "",
                "price": price,
            }

    except Exception as e:
        print(f"Scrape error: {e}")
        return None


async def get_current_price(asin: str) -> float | None:
    try:
        async with async_playwright() as p:
            browser, context = await new_browser_context(p)
            page = await context.new_page()
            await page.goto(f"https://www.amazon.eg/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
            await bypass_bot_check(page)

            try:
                await page.wait_for_selector("span.a-price-whole", timeout=8000)
            except:
                pass

            for selector in [
                "span.priceToPay span.a-price-whole",
                ".apexPriceToPay span.a-price-whole",
                "#corePrice_feature_div span.a-price-whole",
                "span.a-price-whole",
                "#priceblock_ourprice",
                "#priceblock_dealprice",
                ".a-price .a-offscreen",
            ]:
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
    try:
        async with async_playwright() as p:
            browser, context = await new_browser_context(p)
            page = await context.new_page()
            await page.goto(
                f"https://www.amazon.eg/s?k={query.replace(' ', '+')}",
                wait_until="domcontentloaded", timeout=30000
            )
            await bypass_bot_check(page)
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
