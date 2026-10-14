"""Public HLTV pages through real Chrome over HTTPS, in our own anonymous session."""

import asyncio
import json
import time
from pathlib import Path

import httpx
from playwright.async_api import Error, async_playwright
from protego import Protego

from integrations.errors import StopSource
from integrations.hltv import BASE, url

AGENT = "BettyAI/0.1"


async def load_page(address, directory: Path, state):
    address = url(address)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=False, timeout=30000)
        storage_path = directory / "browser.json"
        try:
            storage = json.loads(storage_path.read_text()) if storage_path.exists() else None
            page = await browser.new_page(locale="en-US", timezone_id="UTC", storage_state=storage)

            async def navigate(target, selector=None):
                response = await page.goto(target, wait_until="domcontentloaded", timeout=30000)
                if response is None:
                    raise ValueError("HLTV navigation returned no response")
                state["http_status"] = response.status
                if url(page.url) != target:
                    raise StopSource("HLTV navigation address changed")
                if response.status in (401, 403, 451):
                    scope = "robots.txt " if target.endswith("/robots.txt") else ""
                    raise StopSource(f"HLTV {scope}HTTP {response.status}")
                httpx.Response(
                    response.status, headers=await response.all_headers(), request=httpx.Request("GET", target)
                ).raise_for_status()
                title = (await page.title()).lower()
                if "just a moment" in title or "access denied" in title or "verify" in title:
                    scope = "robots.txt " if target.endswith("/robots.txt") else ""
                    raise StopSource(f"HLTV {scope}visitor challenge")
                if selector:
                    await page.locator(selector).first.wait_for(state="attached", timeout=15000)
                return response

            if state.get("robots_at", 0) + 86400 <= time.time():
                await navigate(BASE + "/robots.txt")
                robots = await page.locator("body").inner_text()
                if "user-agent:" not in robots.lower():
                    raise StopSource("HLTV robots.txt unavailable")
                state.update(robots=robots, robots_at=time.time())
                await asyncio.sleep(max(10, Protego.parse(robots).crawl_delay(AGENT) or 0))
            rules = Protego.parse(state.get("robots", ""))
            if not rules.can_fetch(address, AGENT):
                raise StopSource("HLTV robots.txt disallows requested path")
            if "/team/" in address:
                selector = ".profile-team-name"
            elif "/matches/" in address and "/stats/" not in address:
                selector = ".timeAndEvent"
            elif "/news/" in address:
                selector = "article.newsitem .newstext-con"
            elif "/results" in address:
                selector = ".results-all"
            else:
                selector = ".contentCol"
            await navigate(address, selector)
            html = await page.content()
            if len(html.encode()) > 20 * 1024 * 1024:
                raise ValueError("HLTV page exceeds 20 MiB")
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
            temporary = storage_path.with_suffix(".tmp")
            temporary.touch(mode=0o600, exist_ok=True)
            temporary.chmod(0o600)
            await page.context.storage_state(path=temporary)
            temporary.replace(storage_path)
            return html
        except Error as error:
            raise ValueError(f"HLTV Chrome: {type(error).__name__}") from error
        finally:
            await browser.close()
