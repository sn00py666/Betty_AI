"""Один короткий сеанс Chrome для сайтов, которые отдают линию через JavaScript."""

import asyncio
from urllib.parse import urlsplit

import httpx
from playwright.async_api import Error, async_playwright
from protego import Protego

READY = {
    "leon": '[data-test-el="sportline-runner"]',
    "betboom": '[id^="match-markets-toggle-"]',
}


async def load_rendered(name, url, robots=None):
    """Леон и BetBoom: отдельный видимый Chrome, без личного профиля и подмены браузерных свойств."""
    request = httpx.Request("GET", url)
    denied = None
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=False, timeout=30000)
        except Error as error:
            raise ValueError(f"{name}: нужен установленный Chrome и графический сеанс") from error
        try:
            page = await browser.new_page(locale="ru-RU", timezone_id="Europe/Moscow")

            def check_response(response):
                nonlocal denied
                host = urlsplit(response.url).hostname or ""
                # Ошибка рекламного пикселя не равна блокировке линии.
                relevant = host == urlsplit(url).hostname or host.endswith(".sporthub.bet")
                if relevant and response.status in (401, 403, 429, 451):
                    denied = httpx.Response(response.status, headers=response.headers, request=request)

            page.on("response", check_response)
            if robots is None:
                origin = urlsplit(url)
                response = await page.goto(f"{origin.scheme}://{origin.netloc}/robots.txt", timeout=30000)
                if denied:
                    return denied, "", None
                if response and response.status in (404, 410):
                    robots = ""
                else:
                    if response is None:
                        raise ValueError(f"{name}: robots.txt не загрузился")
                    httpx.Response(response.status, request=request).raise_for_status()
                    # Не принимаем HTML/JS-проверку за пустой robots.txt.
                    robots = await page.locator("body").inner_text()
                    if "user-agent:" not in robots.lower():
                        raise ValueError(f"{name}: вместо robots.txt получена другая страница")
                await asyncio.sleep(max(10, Protego.parse(robots).crawl_delay("BettyAI/0.1") or 0))
            rules = Protego.parse(robots)
            if not rules.can_fetch(url, "BettyAI/0.1"):
                raise ValueError(f"{name}: robots.txt запрещает этот адрес")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if denied:
                return denied, "", robots
            if response is None:
                raise ValueError(f"{name}: страница не загрузилась")
            httpx.Response(response.status, request=request).raise_for_status()
            # DOMContentLoaded ещё не означает, что линия из WebSocket готова.
            for _ in range(60):
                if denied:
                    return denied, "", robots
                body = (await page.locator("body").inner_text()).lower()
                if any(s in body for s in ("verify you are human", "подтвердите, что вы", "forbidden")):
                    raise ValueError(f"{name}: проверка посетителя; сбор остановлен")
                if await page.locator(READY[name]).count():
                    await asyncio.sleep(2)  # Даём странице закончить первый пакет карточек.
                    if denied:
                        return denied, "", robots
                    return httpx.Response(200, request=request), await page.content(), robots
                await asyncio.sleep(0.5)
            raise ValueError(f"{name}: линия не появилась за 30 секунд; проверьте окно Chrome и доступ к сайту")
        except Error as error:
            if denied:
                return denied, "", robots
            raise ValueError(f"{name}: ошибка загрузки Chrome; требуется проверка") from error
        finally:
            await browser.close()


async def load_winline(url):
    denied = None
    request = httpx.Request("GET", url)

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Error as error:
            raise ValueError("Для Winline установите Chrome: uv run playwright install chrome") from error
        try:
            page = await browser.new_page(locale="ru-RU", timezone_id="Europe/Moscow")

            async def route_request(route):
                host = urlsplit(route.request.url).hostname or ""
                if denied or route.request.resource_type in ("image", "font", "media"):
                    await route.abort()
                elif host != "winline.ru" and not host.endswith(".winline.ru"):
                    await route.abort()
                else:
                    await route.continue_()

            def check_response(response):
                nonlocal denied
                if response.status in (401, 403, 429, 451):
                    denied = httpx.Response(response.status, headers=response.headers, request=request)

            await page.route("**/*", route_request)
            page.on("response", check_response)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None:
                raise ValueError("Winline: страница не загрузилась")
            if response.status >= 400:
                return httpx.Response(response.status, headers=response.headers, request=request), ""
            for _ in range(40):
                if denied:
                    return denied, ""
                body = (await page.locator("body").inner_text()).lower()
                if any(marker in body for marker in ("verify you are human", "captcha", "проверка браузера")):
                    raise ValueError("Winline: проверка посетителя, сбор остановлен")
                if await page.locator(".event-card .coefficient-button_generic2").count():
                    # Читаем отрисованную страницу; не исполняем собственный декодер протокола.
                    return httpx.Response(200, request=request), await page.content()
                await asyncio.sleep(0.5)
            raise ValueError("Winline: карточки коэффициентов не загрузились")
        except Error as error:
            if denied:
                return denied, ""
            raise ValueError("Winline: ошибка загрузки Chrome; требуется проверка") from error
        finally:
            await browser.close()
