"""Короткий сеанс Chrome: обе дисциплины, ожидание линии и прокрутка списка."""

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Error, async_playwright
from protego import Protego

READY = {
    "leon": '[data-test-el="sportline-event-block"]',
    "betboom": '[id^="match-markets-toggle-"]',
    "winline": ".event-card .coefficient-button_generic2",
}
BROWSER_URLS = {
    "leon": ["https://leon.ru/esports/cs2", "https://leon.ru/esports/dota-2"],
    "winline": [
        "https://winline.ru/stavki/sport/kibersport/counter-strike",
        "https://winline.ru/stavki/sport/kibersport/dota_2",
    ],
    "betboom": ["https://betboom.ru/esport/counter-strike-2", "https://betboom.ru/esport/dota-2"],
}


def slim_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style,svg,img,link,meta,iframe,noscript"):
        node.decompose()
    return str(soup)


async def snapshots(page, name):
    await page.locator(READY[name]).first.wait_for(state="attached", timeout=30000)
    # Первый коэффициент появляется раньше всей линии (особенно у Winline).
    await asyncio.sleep(10)
    result = []
    for step in range(30):
        if name == "betboom":
            expanded = await page.locator('button[id^="tournament-trigger-"][aria-expanded="false"]').evaluate_all(
                "buttons => { buttons.forEach(button => button.click()); return buttons.length; }"
            )
            if expanded:
                await asyncio.sleep(2)
        result.append(
            dict(url=page.url, captured_at=datetime.now(UTC).isoformat(), html=slim_html(await page.content()))
        )
        if step == 29:
            raise ValueError(f"{name}: список не закончился за 30 прокруток; требуется проверка")
        moved = await page.evaluate(
            """selector => {
            let node = document.querySelector(selector);
            while (node && node !== document.body) {
                const css = getComputedStyle(node);
                if (node.scrollHeight > node.clientHeight + 100 && /auto|scroll/.test(css.overflowY)) {
                    const before = node.scrollTop;
                    node.scrollTop += node.clientHeight * 0.85;
                    return node.scrollTop !== before;
                }
                node = node.parentElement;
            }
            const before = window.scrollY;
            window.scrollBy(0, window.innerHeight * 0.85);
            return window.scrollY !== before;
        }""",
            READY[name],
        )
        if not moved:
            break
        await asyncio.sleep(1.5)
    return result


async def load_rendered(name, url, robots=None):
    request = httpx.Request("GET", url)
    denied = None
    async with async_playwright() as playwright:
        browser = None
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=name == "winline", timeout=30000)
            page = await browser.new_page(locale="ru-RU", timezone_id="Europe/Moscow")

            def check_response(response):
                nonlocal denied
                host = urlsplit(response.url).hostname or ""
                source_host = urlsplit(url).hostname or ""
                relevant = host == source_host or host.endswith(("." + source_host, ".sporthub.bet"))
                if relevant and response.status in (
                    401,
                    403,
                    429,
                    451,
                ):
                    denied = httpx.Response(response.status, headers=response.headers, request=request)

            page.on("response", check_response)
            if name == "winline":

                async def route_request(route):
                    host = urlsplit(route.request.url).hostname or ""
                    if denied or route.request.resource_type in ("image", "font", "media"):
                        await route.abort()
                    elif host != "winline.ru" and not host.endswith(".winline.ru"):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", route_request)
            if robots is None:
                origin = urlsplit(url)
                response = await page.goto(f"{origin.scheme}://{origin.netloc}/robots.txt", timeout=30000)
                if denied:
                    return denied, "", None
                if response is None:
                    raise ValueError(f"{name}: robots.txt не загрузился")
                if response.status in (404, 410):
                    robots = ""
                else:
                    httpx.Response(response.status, request=request).raise_for_status()
                    robots = await page.locator("body").inner_text()
                    if "user-agent:" not in robots.lower():
                        raise ValueError(f"{name}: вместо robots.txt получена другая страница")
                await asyncio.sleep(max(10, Protego.parse(robots).crawl_delay("BettyAI/0.1") or 0))
            rules = Protego.parse(robots)
            pages = []
            for index, address in enumerate(BROWSER_URLS[name]):
                if not rules.can_fetch(address, "BettyAI/0.1"):
                    raise ValueError(f"{name}: robots.txt запрещает {address}")
                if index:
                    await asyncio.sleep(max(10, rules.crawl_delay("BettyAI/0.1") or 0))
                response = await page.goto(address, wait_until="domcontentloaded", timeout=30000)
                if denied:
                    return denied, "", robots
                if response is None:
                    raise ValueError(f"{name}: страница не загрузилась")
                httpx.Response(response.status, request=request).raise_for_status()
                if name == "betboom":
                    # Прямой вход открывает Live даже по адресу прематча.
                    await page.get_by_text("Все", exact=True).click(timeout=30000)
                    await page.wait_for_url(address, timeout=10000)
                pages.extend(await snapshots(page, name))
                if denied:
                    return denied, "", robots
                body = (await page.locator("body").inner_text()).lower()
                if any(
                    s in body for s in ("verify you are human", "подтвердите, что вы", "forbidden", "проверка браузера")
                ):
                    raise ValueError(f"{name}: проверка посетителя; сбор остановлен")
            return httpx.Response(200, request=request), json.dumps({"pages": pages}, ensure_ascii=False), robots
        except Error as error:
            if denied:
                return denied, "", robots
            raise ValueError(f"{name}: линия не загрузилась в Chrome; требуется проверка") from error
        finally:
            if browser:
                await browser.close()


async def load_winline(url, robots=""):
    response, text, _ = await load_rendered("winline", url, robots)
    return response, text
