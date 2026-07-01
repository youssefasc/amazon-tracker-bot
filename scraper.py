import re
import asyncio
import random
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
            c = m.group(1)
            if c.startswith("B") or c.isdigit():
                return c
    return None


async def _open_and_read(url: str) -> dict | None:
    """EXACT same logic as the working /debug command"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ar-EG",
            extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            print(f"HTTP: {resp.status if resp else 'None'}, URL: {page.url[:60]}")
            # Bot check
            try:
                btn = await page.query_selector("input[type='submit'], button[type='submit'], .a-button-input")
                if btn:
                    await btn.click()
                    await asyncio.sleep(3)
            except:
                pass
            # Wait for redirect
            for _ in range(10):
                await asyncio.sleep(1)
                if "amazon.eg" in page.url and "/dp/" in page.url:
                    break
            print(f"Final URL: {page.url[:70]}")
            asin = extract_asin(page.url)
            # Title
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
            # Price
            price = None
            for sel in ["span.priceToPay span.a-price-whole", ".apexPriceToPay span.a-price-whole",
                        "span.a-price-whole", "#corePrice_feature_div span.a-price-whole",
                        "#priceblock_ourprice", ".a-price .a-offscreen"]:
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
            # Image
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
            # Original price (before discount)
            original_price = None
            for sel in ["span.a-price.a-text-price span.a-offscreen", ".basisPrice span.a-offscreen"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        raw = (await el.inner_text()).strip()
                        cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
                        if cleaned:
                            original_price = float(cleaned)
                            break
                except:
                    pass
            # Discount percent
            discount_pct = None
            try:
                el = await page.query_selector(".savingsPercentage, .a-color-price")
                if el:
                    txt = (await el.inner_text()).strip()
                    m = re.search(r"(\d+)%", txt)
                    if m:
                        discount_pct = int(m.group(1))
            except:
                pass
            print(f"Result — asin:{asin}, title:{bool(title)}, price:{price}")
            return {
                "asin": asin, "title": title, "price": price,
                "image_url": image_url, "original_price": original_price,
                "discount_pct": discount_pct
            }
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


async def get_prices_batch(asins: list[str]) -> dict[str, float]:
    """Check prices for multiple ASINs using ONE browser (saves resources).
    Reads ONLY the main price container to avoid picking up prices from other products."""
    results = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ar-EG",
                extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()
            for asin in asins:
                try:
                    await page.goto(f"https://www.amazon.eg/dp/{asin}",
                                    wait_until="domcontentloaded", timeout=25000)
                    try:
                        btn = await page.query_selector("input[type='submit'], button[type='submit'], .a-button-input")
                        if btn:
                            await btn.click()
                            await asyncio.sleep(2)
                    except:
                        pass
                    # استنى لحد ما الصفحة الصح تتحمّل (لازم يكون فيها /dp/ والـ ASIN الصح)
                    loaded = False
                    for _ in range(8):
                        await asyncio.sleep(1)
                        if "amazon.eg" in page.url and asin in page.url:
                            loaded = True
                            break
                    if not loaded:
                        print(f"Batch price {asin}: page didn't load correctly, skipping")
                        continue

                    # تأكد إن عنوان المنتج موجود (تأكيد إننا في صفحة منتج صح)
                    title_el = await page.query_selector("#productTitle")
                    if not title_el:
                        print(f"Batch price {asin}: no product title, skipping")
                        continue

                    price = await _read_main_price(page)
                    if price and price > 0:
                        results[asin] = price
                        print(f"Batch price {asin}: {price}")
                    else:
                        print(f"Batch price {asin}: no valid price found, skipping")
                except Exception as e:
                    print(f"Batch price error {asin}: {e}")
                    continue
            await browser.close()
    except Exception as e:
        print(f"Batch error: {e}")
    return results


async def _read_main_price(page) -> float | None:
    """Read price ONLY from the main product price container (never from related products)."""
    # الحاويات الرسمية لسعر المنتج الرئيسي فقط
    main_containers = [
        "#corePriceDisplay_desktop_feature_div",
        "#corePrice_feature_div",
        "#apex_desktop",
        "#corePriceDisplay_mobile_feature_div",
        "#buybox",
    ]
    for container_sel in main_containers:
        try:
            container = await page.query_selector(container_sel)
            if not container:
                continue
            # جرّب السعر المدفوع (priceToPay) أولاً — ده السعر الحالي الفعلي
            for sel in ["span.priceToPay span.a-price-whole",
                        ".a-price:not(.a-text-price) span.a-price-whole",
                        "span.a-price-whole"]:
                try:
                    el = await container.query_selector(sel)
                    if el:
                        raw = (await el.inner_text()).strip()
                        cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
                        if cleaned:
                            val = float(cleaned)
                            if val > 0:
                                return val
                except:
                    pass
        except:
            pass
    return None


async def search_amazon(query: str) -> list[dict]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"])
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
                    image_url = ""
                    for img_sel in ["img.s-image", ".s-product-image-container img"]:
                        try:
                            img_el = await item.query_selector(img_sel)
                            if img_el:
                                image_url = await img_el.get_attribute("src") or ""
                                if image_url:
                                    break
                        except:
                            pass
                    rating = ""
                    try:
                        rating_el = await item.query_selector("span.a-icon-alt")
                        if rating_el:
                            txt = (await rating_el.inner_text()).strip()
                            rating = txt.split(" ")[0] if txt else ""
                    except:
                        pass
                    if title and price:
                        results.append({
                            "asin": asin, "title": title[:150], "price": price,
                            "url": f"https://www.amazon.eg/dp/{asin}",
                            "affiliate_url": make_affiliate_url(asin),
                            "image_url": image_url, "rating": rating,
                        })
                except:
                    continue
            await browser.close()
            return results
    except Exception as e:
        print(f"Search error: {e}")
        return []


async def get_product_screenshot(asin: str) -> bytes | None:
    """Take a screenshot of the product page"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ar-EG",
                viewport={"width": 1280, "height": 1024},
                extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()
            await page.goto(f"https://www.amazon.eg/dp/{asin}", wait_until="domcontentloaded", timeout=30000)

            # Handle bot check — click and wait for redirect to product
            try:
                btn = await page.query_selector("input[type='submit'], button[type='submit'], .a-button-input")
                if btn:
                    await btn.click()
                    # استنى لحد ما المنتج يظهر فعلاً
                    for _ in range(10):
                        await asyncio.sleep(1)
                        title_el = await page.query_selector("#productTitle")
                        if title_el:
                            break
            except:
                pass

            # تأكد إن صفحة المنتج ظهرت (مش صفحة الـ bot check)
            try:
                await page.wait_for_selector("#productTitle", timeout=10000)
            except:
                # لو مفيش productTitle يبقى لسه على صفحة bot check — مينفعش screenshot
                await browser.close()
                return None

            await asyncio.sleep(2)  # استنى الصور تحمّل

            # اقفل أي popups
            try:
                close_btn = await page.query_selector("[data-action='a-popover-close'], .a-button-close")
                if close_btn:
                    await close_btn.click()
                    await asyncio.sleep(1)
            except:
                pass

            screenshot = await page.screenshot(clip={"x": 0, "y": 0, "width": 1280, "height": 700})
            await browser.close()
            return screenshot
    except Exception as e:
        print(f"Screenshot error: {e}")
        return None


CATEGORY_URLS = {
    "electronics": [
        "https://www.amazon.eg/s?k=mobile+phones&i=electronics",
        "https://www.amazon.eg/s?k=laptop&i=electronics",
        "https://www.amazon.eg/s?k=headphones&i=electronics",
        "https://www.amazon.eg/s?k=smart+watch&i=electronics",
        "https://www.amazon.eg/s?k=tablet&i=electronics",
        "https://www.amazon.eg/s?k=power+bank&i=electronics",
        "https://www.amazon.eg/s?k=electronics",
    ],
    "appliances": [
        "https://www.amazon.eg/s?k=home+appliances",
        "https://www.amazon.eg/s?k=kitchen+appliances",
        "https://www.amazon.eg/s?k=air+conditioner",
        "https://www.amazon.eg/s?k=refrigerator",
        "https://www.amazon.eg/s?k=washing+machine",
        "https://www.amazon.eg/s?k=microwave",
        "https://www.amazon.eg/s?k=vacuum+cleaner",
    ],
    "fashion": [
        "https://www.amazon.eg/s?k=fashion+clothing",
        "https://www.amazon.eg/s?k=clothes",
        "https://www.amazon.eg/s?k=shoes",
        "https://www.amazon.eg/s?k=watches",
        "https://www.amazon.eg/s?k=bags",
        "https://www.amazon.eg/s?k=perfume",
        "https://www.amazon.eg/s?k=sunglasses",
    ],
    "grocery": [
        "https://www.amazon.eg/s?k=grocery",
        "https://www.amazon.eg/s?k=snacks",
        "https://www.amazon.eg/s?k=coffee",
        "https://www.amazon.eg/s?k=supplements",
    ],
    "general": [
        "https://www.amazon.eg/s?k=deals",
        "https://www.amazon.eg/s?k=offers",
        "https://www.amazon.eg/s?k=best+sellers",
        "https://www.amazon.eg/s?k=toys",
        "https://www.amazon.eg/s?k=beauty",
        "https://www.amazon.eg/s?k=sports",
        "https://www.amazon.eg/s?k=home",
        "https://www.amazon.eg/s?k=books",
        "https://www.amazon.eg/s?k=games",
        "https://www.amazon.eg/s?k=kitchen",
        "https://www.amazon.eg/s?k=baby",
        "https://www.amazon.eg/s?k=car+accessories",
        "https://www.amazon.eg/s?k=tools",
        "https://www.amazon.eg/s?k=pet+supplies",
        "https://www.amazon.eg/s?k=office",
        "https://www.amazon.eg/s?k=garden",
        "https://www.amazon.eg/s?k=health",
        "https://www.amazon.eg/s?k=gaming",
    ],
}

# ترتيب الفئات والعدد المطلوب من كل فئة قبل الانتقال للي بعدها
CATEGORY_ROTATION = (
    [("electronics", 5)] +
    [("appliances", 5)] +
    [("fashion", 5)] +
    [("grocery", 5)] +
    [("general", 100)]
)


async def get_deals_from_amazon(category: str = None, skip_asins: set = None) -> list[dict]:
    """Scrape Amazon Egypt for discounted products. If category given, search it;
    otherwise search all categories as fallback.
    skip_asins: ASINs already posted — skipped BEFORE reading their details (saves time)."""
    skip_asins = skip_asins or set()
    if category and category in CATEGORY_URLS:
        deal_urls = list(CATEGORY_URLS[category])
        random.shuffle(deal_urls)
        # أضف باقي الفئات كـ fallback لو القسم المطلوب مفهوش عروض
        other_urls = []
        for cat, urls in CATEGORY_URLS.items():
            if cat != category:
                other_urls.extend(urls)
        random.shuffle(other_urls)
        deal_urls.extend(other_urls)
    else:
        # كل الفئات
        deal_urls = []
        for urls in CATEGORY_URLS.values():
            deal_urls.extend(urls)
        random.shuffle(deal_urls)

    # أضف رقم صفحة عشوائي لكل رابط عشان نوصل لمنتجات أعمق (مش بس الصفحة الأولى)
    paged_urls = []
    for u in deal_urls:
        pg = random.choice([1, 1, 2, 2, 3])  # أغلبية صفحة 1-2 وأحياناً 3
        sep = "&" if "?" in u else "?"
        if pg > 1:
            paged_urls.append(f"{u}{sep}page={pg}")
        else:
            paged_urls.append(u)
    deal_urls = paged_urls
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"])
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="ar-EG",
                extra_http_headers={"Accept-Language": "ar-EG,ar;q=0.9,en;q=0.8"},
            )
            page = await context.new_page()
            deals = []

            for deal_url in deal_urls:
                if len(deals) >= 15:
                    break
                try:
                    await page.goto(deal_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        btn = await page.query_selector("input[type='submit'], .a-button-input")
                        if btn:
                            await btn.click()
                            await asyncio.sleep(3)
                    except:
                        pass
                    await asyncio.sleep(2)

                    items = await page.query_selector_all("[data-component-type='s-search-result']")
                    print(f"Deals: {deal_url[:40]} → {len(items)} items")

                    for item in items[:15]:
                        if len(deals) >= 15:
                            break
                        try:
                            asin = await item.get_attribute("data-asin") or ""
                            if not asin:
                                link = await item.query_selector("a[href*='/dp/']")
                                if link:
                                    href = await link.get_attribute("href") or ""
                                    asin = extract_asin(href) or ""
                            if not asin or any(d["asin"] == asin for d in deals):
                                continue

                            # تخطي المنتجات المنشورة في آخر 48 ساعة قبل قراءة أي تفاصيل
                            if asin in skip_asins:
                                continue

                            title_el = await item.query_selector("h2 span, h2 a span, .a-text-normal")
                            title = (await title_el.inner_text()).strip() if title_el else ""
                            if not title:
                                continue

                            price_el = await item.query_selector("span.a-price span.a-offscreen, span.a-price-whole")
                            price_raw = (await price_el.inner_text()).strip() if price_el else ""
                            price_clean = re.sub(r"[^\d.]", "", price_raw.replace(",", ""))
                            price = float(price_clean) if price_clean else None
                            if not price:
                                continue

                            # Original price (strikethrough)
                            orig_el = await item.query_selector("span.a-price.a-text-price span.a-offscreen")
                            orig_raw = (await orig_el.inner_text()).strip() if orig_el else ""
                            orig_clean = re.sub(r"[^\d.]", "", orig_raw.replace(",", ""))
                            original_price = float(orig_clean) if orig_clean else None

                            discount_pct = None
                            # الخصم لازم يتحسب من السعر المشطوب الحقيقي فقط
                            if original_price and original_price > price:
                                discount_pct = int((original_price - price) / original_price * 100)

                            # لازم يكون عليه خصم بين 25% و 80% (فوق 80% غالباً سعر غلط)
                            if not discount_pct or discount_pct < 25 or discount_pct > 80:
                                continue

                            # تأكد إن السعرين منطقيين (مش أرقام غريبة)
                            if price < 10 or (original_price and original_price < price):
                                continue

                            img_el = await item.query_selector("img.s-image, img")
                            image_url = (await img_el.get_attribute("src") or "") if img_el else ""

                            deals.append({
                                "asin": asin, "title": title[:200], "price": price,
                                "original_price": original_price, "discount_pct": discount_pct,
                                "image_url": image_url,
                                "affiliate_url": make_affiliate_url(asin),
                            })
                        except:
                            continue
                except Exception as e:
                    print(f"Deal URL error ({deal_url[:40]}): {e}")
                    continue

            await browser.close()
            random.shuffle(deals)
            print(f"Found {len(deals)} deals")
            return deals
    except Exception as e:
        print(f"Deals error: {e}")
        return []

