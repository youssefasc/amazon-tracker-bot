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
AMAZON_DOMAINS = ["amazon.eg", "amazon.com", "amazon.co.uk", "amazon.de"]


async def make_browser():
    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
    )
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        locale="ar-EG",
        extra_http_headers={
            "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        },
        viewport={"width": 1280, "height": 800},
    )
    # Remove webdriver flag
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['ar-EG', 'ar', 'en']});
    """)
    return p, browser, context


async def handle_bot_check(page) -> bool:
    """Click continue if bot check appears — WITHOUT navigating away"""
    try:
        for selector in ["input[type='submit']", "button[type='submit']", ".a-button-input", "[name='continue']"]:
            el = await page.query_selector(selector)
            if el:
                print("Bot check — clicking continue")
                await el.click()
                await asyncio.sleep(3)
                return True
    except:
        pass
    return False


async def read_product_data(page) -> dict:
    """Read title, price, image from current page"""
    # Wait for title
    try:
        await page.wait_for_selector("#productTitle", timeout=8000)
    except:
        pass
    await asyncio.sleep(1)

    title = None
    for sel in ["#productTitle", "span#productTitle"]:
        try:
            el = await page.query_selector(sel)
            if el:
                t = (await el.inner_text()).strip()
                if t:
                    title = t
                    break
        except:
            pass

    price = None
    for sel in [
        "span.priceToPay span.a-price-whole",
        ".apexPriceToPay span.a-price-whole",
        "#corePrice_feature_div span.a-price-whole",
        "span.a-price-whole",
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

    image_url = None
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

    return {"title": title, "price": price, "image_url": image_url or ""}


async def scrape_amazon_product(url: str) -> dict | None:
    p, browser, context = await make_browser()
    try:
        page = await context.new_page()

        # Step 1: Open the URL
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"HTTP: {resp.status if resp else '?'} | URL: {page.url[:70]}")

        # Step 2: Handle bot check if on non-product page
        if "/dp/" not in page.url and "/gp/" not in page.url:
            await handle_bot_check(page)

        # Step 3: Wait for redirect to product page
        needs_resolve = any(d in url for d in REDIRECT_DOMAINS)
        if needs_resolve:
            for _ in range(12):
                await asyncio.sleep(1)
                cur = page.url
                if any(d in cur for d in AMAZON_DOMAINS) and "/dp/" in cur:
                    print(f"Landed on product: {cur[:70]}")
                    break
                # Bot check mid-redirect
                if any(d in cur for d in AMAZON_DOMAINS) and "/dp/" not in cur:
                    await handle_bot_check(page)

        # Step 4: If still not on product page but have ASIN, navigate there
        asin = extract_asin(page.url)
        if not asin:
            print(f"No ASIN in: {page.url}")
            return None

        if "/dp/" not in page.url:
            print(f"Navigating to product with ASIN: {asin}")
            await page.goto(f"https://www.amazon.eg/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
            await handle_bot_check(page)

        # Step 5: Read from THIS page (same session, same cookies)
        data = await read_product_data(page)
        print(f"Result — title: {bool(data['title'])}, price: {data['price']}")

        if not data["title"] or not data["price"]:
            return None

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
    finally:
        await browser.close()
        await p.stop()


async def get_current_price(asin: str) -> float | None:
    p, browser, context = await make_browser()
    try:
        page = await context.new_page()
        await page.goto(f"https://www.amazon.eg/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
        await handle_bot_check(page)
        try:
            await page.wait_for_selector("span.a-price-whole", timeout=8000)
        except:
            pass
        for sel in [
            "span.priceToPay span.a-price-whole",
            ".apexPriceToPay span.a-price-whole",
            "#corePrice_feature_div span.a-price-whole",
            "span.a-price-whole",
        ]:
            try:
                el = await page.query_selector(sel)
                if el:
                    raw = (await el.inner_text()).strip()
                    cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
                    if cleaned:
                        return float(cleaned)
            except:
                pass
        return None
    except Exception as e:
        print(f"Price error: {e}")
        return None
    finally:
        await browser.close()
        await p.stop()


async def search_amazon(query: str) -> list[dict]:
    p, browser, context = await make_browser()
    try:
        page = await context.new_page()
        await page.goto(
            f"https://www.amazon.eg/s?k={query.replace(' ', '+')}",
            wait_until="domcontentloaded", timeout=30000
        )
        await handle_bot_check(page)
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
        return results
    except Exception as e:
        print(f"Search error: {e}")
        return []
    finally:
        await browser.close()
        await p.stop()
