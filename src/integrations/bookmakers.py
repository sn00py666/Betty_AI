"""CS2 / Dota 2: победитель всей серии; live и прематч помечены отдельно."""

import json
import math
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from integrations.dates import start_time

SOURCES = {
    "winline": "https://winline.ru/stavki/sport/kibersport/counter-strike",
    "pari": "https://line-lb01-w.pb06e2-resources.com/events/listBase?lang=ru&scopeMarket=2300",
    "fonbet": "https://line-lb51.bk6bba-resources.com/events/list?lang=ru&scopeMarket=1600",
    "marathon": "https://www.marathonbet.ru/su/betting/e-Sports+-+1895085",
    "betboom": "https://betboom.ru/esport/counter-strike-2",
    "leon": "https://leon.ru/esports/cs2",
}


def price(value):
    """Отсутствующая или закрытая котировка не становится нулевым коэффициентом."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 1 else None


def parse_fonbet(text, bookmaker="fonbet"):
    domain = "pari.ru" if bookmaker == "pari" else "fon.bet"
    data = json.loads(text)
    if not all(isinstance(data.get(key), list) for key in ("sports", "events", "customFactors")):
        raise ValueError(f"Изменился формат линии {bookmaker}")
    tournaments = {
        s["id"]: s["name"]
        for s in data["sports"]
        if "Counter-Strike" in s.get("name", "") or s.get("name", "").startswith("Dota 2.")
    }
    factors = {f["e"]: f for f in data["customFactors"]}
    matches = []
    for event in data["events"]:
        if event.get("sportId") not in tournaments or event.get("level") != 1 or event.get("place") != "line":
            continue
        if not event.get("team1") or not event.get("team2"):
            continue
        market = factors.get(event["id"], {})
        odds = {f["f"]: price(f.get("v")) for f in market.get("factors", []) if not f.get("blocked")}
        suspended = bool(event.get("blocked") or market.get("blocked"))
        tournament = tournaments[event["sportId"]]
        best_of = re.search(r"\bBo([135])\b", tournament, re.IGNORECASE)
        sport = "dota2" if tournament.startswith("Dota 2.") else "cs2"
        if sport == "dota2" and (not best_of or 922 in odds):
            continue  # Не сравниваем двухисходный рынок с BO2/ничьей.
        matches.append(
            {
                "id": str(event["id"]),
                "sport": sport,
                "team1": event["team1"],
                "team2": event["team2"],
                "tournament": tournament,
                "best_of": int(best_of[1]) if best_of else None,
                "starts_at": datetime.fromtimestamp(event["startTime"], UTC).isoformat(),
                "start_label": None,
                "market": "match_winner",
                "status": "prematch",
                "p1": None if suspended else odds.get(921),
                "p2": None if suspended else odds.get(923),
                "url": f"https://{domain}/sports/{event['sportId']}/{event['id']}",
            }
        )
    return matches


def parse_marathon(text, captured_at=None, url=None):
    soup = BeautifulSoup(text, "html.parser")
    if not soup.select_one(".category-container"):
        raise ValueError("Линия Марафон отсутствует или изменилась разметка")
    zone = re.search(r'"timeZoneId"\s*:\s*"([^"]+)"', text)
    # Предпочитаем структурированные даты; текст — только с часовым поясом самой страницы.
    dates = {}
    for script in soup.select('script[type="application/ld+json"]'):
        entries = json.loads(script.get_text())
        for entry in entries if isinstance(entries, list) else [entries]:
            if entry.get("@type") == "SportsEvent":
                value = entry.get("startDate")
                if value:
                    # В HTML Марафона UTC записан как +000 вместо +0000.
                    value = value + "0" if value.endswith("+000") else value
                    start = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
                    dates[urlsplit(entry.get("url", "")).path] = start.astimezone(UTC).isoformat()
    matches = []
    for category in soup.select(".category-container"):
        heading = category.select_one(".category-label")
        tournament = heading.get_text(" ", strip=True) if heading else ""
        if not re.search(r"\b(?:CS\s*2|Dota\s*2)\.", tournament, re.I):
            continue
        for row in category.select('[data-event-name][data-live="false"]'):
            teams = [node.get_text(strip=True) for node in row.select('[data-member-link="true"]')]
            if len(teams) != 2:
                raise ValueError("Не удалось прочитать две команды Марафон")
            event_id = row["data-event-eventid"]
            if row.select_one(f'[data-selection-key="{event_id}@Match_Result.2"]'):
                continue  # Трёхисходный рынок с ничьей не сравниваем с победителем серии.
            odds = {}
            for node in row.select("[data-selection-key]"):
                key = node["data-selection-key"]
                if key in (f"{event_id}@Match_Result.1", f"{event_id}@Match_Result.3"):
                    odds[key.rsplit(".", 1)[1]] = (
                        price(node.get("data-selection-price")) if "active-selection" in node.get("class", []) else None
                    )
            path = "/su/betting/" + row["data-event-path"]
            label = row.select_one(".date")
            start_label = label.get_text(" ", strip=True) if label else None
            dated_label = start_label
            if start_label and re.fullmatch(r"\d{1,2}:\d{2}", start_label) and row.select_one(".today-member-name"):
                dated_label = "сегодня " + start_label
            best_of = re.search(r"из ([135])", tournament)
            matches.append(
                {
                    "id": str(event_id),
                    "team1": teams[0],
                    "team2": teams[1],
                    "tournament": tournament,
                    "best_of": int(best_of[1]) if best_of else (1 if "одной карты" in tournament else None),
                    "sport": "dota2" if "dota" in tournament.lower() else "cs2",
                    "starts_at": dates.get(path) or (start_time(dated_label, captured_at, zone[1]) if zone else None),
                    "start_label": start_label,
                    "market": "match_winner",
                    "status": "prematch",
                    "p1": odds.get("1"),
                    "p2": odds.get("3"),
                    "url": "https://www.marathonbet.ru" + path,
                }
            )
    return matches


def parse_pari(text):
    return parse_fonbet(text, bookmaker="pari")


def parse_winline(text, captured_at=None, url=None):
    soup = BeautifulSoup(text, "html.parser")
    if not soup.select_one("ww-feature-block-tournament-dsk"):
        raise ValueError("Winline: не найдены турниры")
    matches = []
    for tournament in soup.select("ww-feature-block-tournament-dsk"):
        heading = tournament.select_one(".block-tournament-header__title")
        title = heading.get_text(" ", strip=True) if heading else ""
        if not any(game in title.lower() for game in ("counter-strike", "dota")) or "дуэль" in title.lower():
            continue
        for card in tournament.select(".event-card"):
            teams = card.select(".body-left__names > .name")
            row = card.select_one(".card__body:not(.card__body--second)")
            label = row.select_one(".match-row-label") if row else None
            if len(teams) != 2 or not label or label.get_text(strip=True) != "Матч":
                raise ValueError("Winline: изменилась разметка матча")
            market = row.select_one(".card__market")
            buttons = market.select(".coefficient-button_generic2") if market else []
            if len(buttons) != 2:
                continue  # Не подставляем фору, тотал или цену отдельной карты.
            odds = [
                None if "disabled" in " ".join(button.get("class", [])) else price(button.get_text(strip=True))
                for button in buttons
            ]
            start = card.select_one(".header-left__time")
            live = card.select_one(".card--live") is not None
            event_id = card["id"].removeprefix("eventId-")
            matches.append(
                {
                    "id": event_id,
                    "team1": teams[0].get_text(strip=True),
                    "team2": teams[1].get_text(strip=True),
                    "tournament": title,
                    "sport": "dota2" if "dota" in title.lower() else "cs2",
                    "best_of": None,
                    "starts_at": start_time(start.get_text(strip=True), captured_at) if start and not live else None,
                    "start_label": start.get_text(strip=True) if start and not live else None,
                    "live_period": start.get_text(strip=True) if start and live else None,
                    "status": "live" if live else "prematch",
                    "market": "match_winner",
                    "p1": odds[0],
                    "p2": odds[1],
                    "url": f"https://winline.ru/stavki/event/{event_id}",
                }
            )
    return matches


def parse_leon(text, captured_at=None, url=None):
    soup = BeautifulSoup(text, "html.parser")
    cards = soup.select('[data-test-el="sportline-event-block"]')
    if not cards:
        raise ValueError("Леон: карточки матчей не найдены")
    matches = {}
    for card in cards:
        link = card.select_one('a[href^="/bets/esports/cs2/"], a[href^="/bets/esports/dota-2/"]')
        if not link or card.get("data-test-attr-outright") == "true":
            continue
        teams = card.select('[class*="event-card-team-name_"]')
        league = card.find_parent(class_=re.compile(r"^league-element-inner__holder_"))
        title = league.select_one('[class*="list-league-headline-primary-label_"]') if league else None
        if len(teams) != 2 or not title:
            raise ValueError("Леон: изменилась разметка команд или турнира")
        odds = {}
        draw = False
        for market in card.select('[class*=" markets-swiper_"]'):
            label = market.select_one('[class*="card-market-name__text_"]')
            if not label or label.get_text(strip=True) != "Победитель":
                continue
            for button in market.select('[data-test-el="sportline-runner"]'):
                side = button.select_one('[data-test-el="sportline-runner-handicap"]')
                value = button.select_one('[data-test-el="sportline-runner-price"]')
                if side and side.get_text(strip=True).upper() in ("X", "Х", "Н"):
                    draw = True
                if side and side.get_text(strip=True) in ("1", "2"):
                    locked = button.get("data-test-attr-locked") != "false" or button.has_attr("disabled")
                    odds[side.get_text(strip=True)] = None if locked or not value else price(value.get_text(strip=True))
        if draw:
            continue
        live = card.get("data-test-attr-live") == "true"
        label = card.select_one('[class*="sportline-event-card-meta-info-kickoff_"]')
        period = card.select_one('[class*="sportline-event-card-meta-info-stage-label_"]')
        event_id = link["href"].rsplit("/", 1)[1].split("-", 1)[0]
        matches[event_id] = {
            "id": event_id,
            "team1": teams[0].get_text(strip=True),
            "team2": teams[1].get_text(strip=True),
            "tournament": title.get_text(strip=True),
            "sport": "dota2" if "/dota-2/" in link["href"] else "cs2",
            "best_of": None,
            "starts_at": start_time(label.get_text(" ", strip=True), captured_at) if label and not live else None,
            "start_label": label.get_text(" ", strip=True) if label and not live else None,
            "live_period": period.get_text(strip=True) if period and live else None,
            "status": "live" if live else "prematch",
            "market": "match_winner",
            "p1": odds.get("1"),
            "p2": odds.get("2"),
            "url": "https://leon.ru" + link["href"],
        }
    return list(matches.values())


def parse_betboom(text, captured_at=None, url=None):
    """Основной рынок «Исход» в линии CS2 / Dota 2."""
    soup = BeautifulSoup(text, "html.parser")
    heading = soup.select_one("h3.bb--s")
    selected = soup.select_one('[role="radio"][aria-label="Исход"][aria-checked="true"]')
    if not heading or heading.get_text(strip=True) not in ("CS2", "Dota 2") or not selected:
        raise ValueError("BetBoom: не найдена линия CS2 / Dota 2 с выбранным рынком «Исход»")
    # При прокрутке закреплённый заголовок относится к видимой области, а не к первым DOM-карточкам.
    for sticky in soup.select(".bb-Ny"):
        sticky.decompose()
    matches = {}
    for toggle in soup.select('[id^="match-markets-toggle-"]'):
        card = toggle.find_parent(class_="bb-nU")
        teams = card.select(".bb-yO") if card else []
        title = card.find_previous("h3", class_="bb-Ls") if card else None
        if len(teams) == 1 and teams[0].get_text(strip=True).startswith("Итоги "):
            continue  # Победитель турнира, не отдельного матча.
        if len(teams) != 2:
            raise ValueError("BetBoom: изменилась разметка команд")
        if not title:
            continue  # Заголовок уже ушёл из виртуального DOM; карточка есть в предыдущем снимке.
        if re.search(r"1x1|дуэл", title.get_text(" ", strip=True), re.I):
            continue
        odds = {}
        if any(node.get_text(strip=True).upper() in ("X", "Х", "Н") for node in toggle.parent.select(".bb-Uu")):
            continue
        # Только основной ряд: раскрытая роспись карт не входит в этот контейнер.
        for button in toggle.parent.select("button"):
            side, value = button.select_one(".bb-Uu"), button.select_one(".bb-Vu")
            if side and value and side.get_text(strip=True) in ("П1", "П2"):
                locked = button.has_attr("disabled") or button.get("aria-disabled") == "true"
                odds[side.get_text(strip=True)] = None if locked else price(value.get_text(strip=True))
        period = card.select_one("time")
        event_id = toggle["id"].removeprefix("match-markets-toggle-")
        label = period.get_text(" ", strip=True) if period else ""
        live = "/live/" in (url or "") or "карта" in label.lower()
        sport = "dota2" if heading.get_text(strip=True) == "Dota 2" else "cs2"
        matches[event_id] = {
            "id": event_id,
            "team1": teams[0].get_text(strip=True),
            "team2": teams[1].get_text(strip=True),
            "tournament": title.get_text(" ", strip=True),
            "sport": sport,
            "best_of": None,
            "starts_at": start_time(label, captured_at) if not live else None,
            "start_label": label if not live else None,
            "live_period": label if live else None,
            "status": "live" if live else "prematch",
            "market": "match_winner",
            "p1": odds.get("П1"),
            "p2": odds.get("П2"),
            # У карточки нет href: не придумываем прямую ссылку на матч.
            "url": url or SOURCES["betboom"],
        }
    return list(matches.values())


PARSERS = {
    "pari": parse_pari,
    "fonbet": parse_fonbet,
    "marathon": parse_marathon,
    "winline": parse_winline,
    "leon": parse_leon,
    "betboom": parse_betboom,
}


def parse_response(name, text, captured_at=None):
    if name in ("fonbet", "pari"):
        return PARSERS[name](text)
    pages = (
        json.loads(text)["pages"]
        if text.lstrip().startswith('{"pages":')
        else [dict(html=text, captured_at=captured_at, url=SOURCES[name])]
    )
    matches = {}
    for page in pages:
        for match in PARSERS[name](page["html"], page["captured_at"], page["url"]):
            match["fetched_at"] = page["captured_at"]
            matches[(match.get("sport", "cs2"), match["id"])] = match
    return list(matches.values())
