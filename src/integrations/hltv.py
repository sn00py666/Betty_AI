"""HLTV public HTML → typed facts. No AI briefings, forum posts, odds or inferred results.

Selectors inspected on the public Spirit profile and Aurora–Spirit match, 2026-10-15.
All profile tabs are present in one document, including their hidden content.
"""

import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

BASE = "https://www.hltv.org"
TABS = ("infoBox", "rosterBox", "matchesBox", "eventsBox", "achievementsBox", "newsBox", "statsBox")
VERSION = 1


def text(node):
    return node.get_text(" ", strip=True) if node else ""


def number(value):
    found = re.fullmatch(r"[+#]?(-?\d+(?:\.\d+)?)%?", str(value).strip())
    return float(found[1]) if found and "." in found[1] else int(found[1]) if found else None


def url(value):
    parsed = urlsplit(urljoin(BASE, value or ""))
    if parsed.scheme != "https" or parsed.netloc != "www.hltv.org" or parsed.username:
        raise ValueError("Expected a public HLTV URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def entity(node, kind):
    if node is None:
        return None
    link = node if node.name == "a" else node.select_one(f'a[href^="/{kind}/"]')
    if link is None:
        return None
    address = url(link.get("href"))
    found = re.fullmatch(rf"/{kind}/(\d+)/[^/]+", urlsplit(address).path)
    if not found:
        return None
    label = text(link.select_one(".player-nick, .playersBox-playernick, .teamName, .team-name")) or text(link)
    if not label and (img := link.select_one("img")):
        label = img.get("title") or img.get("alt", "")
    return {"id": int(found[1]), "name": label, "url": address}


def timestamp(node):
    node = node if node and node.has_attr("data-unix") else node.select_one("[data-unix]") if node else None
    if not node:
        return None
    try:
        return datetime.fromtimestamp(int(node["data-unix"]) / 1000, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def soup_for(html):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("#challenge-form, #cf-challenge-running"):
        raise ValueError("HLTV visitor challenge")
    for node in soup.select("script,style,iframe,noscript"):
        node.decompose()
    return soup


def pairs(node, selector):
    result = {}
    for row in node.select(selector):
        cells = row.find_all(recursive=False)
        if len(cells) == 2:
            result[text(cells[0])] = text(cells[1])
    return result


def parse_team(html, address):
    soup = soup_for(html)
    address = url(address)
    found = re.fullmatch(r"/team/(\d+)/[^/]+", urlsplit(address).path)
    name = text(soup.select_one(".profile-team-name"))
    if not found or not name or any(soup.select_one(f"#{tab}") is None for tab in TABS):
        raise ValueError("Incomplete HLTV team profile")
    team_id = int(found[1])
    flag = soup.select_one(".profile-team-info .flag")
    country = re.search(r"/([A-Z]{2})\.", flag.get("src", "")) if flag else None
    ranks = {}
    for key, selector in (
        ("hltv", f'a[href*="/ranking/teams/"][href$="/details/{team_id}"]'),
        ("valve", f'a[href="/valve-ranking/teams?teamId={team_id}"]'),
    ):
        link = soup.select_one(selector)
        if link and (rank := number(text(link))) is not None:
            ranks[key] = {"rank": rank, "url": url(link["href"])}
    roster = []
    for row in soup.select("#rosterBox .players-table tbody tr"):
        player = entity(row, "player")
        cells = row.select("td")
        if not player or len(cells) < 5:
            raise ValueError("Changed HLTV roster row")
        rating = row.select_one(".rating-cell")
        version = rating.select_one("[title]") if rating else None
        default_version = text(soup.select_one("#rosterBox .playersBox-rating"))
        roster.append(
            player
            | {
                "status": text(row.select_one(".player-status")),
                "tenure": text(cells[2]),
                "maps_played": number(text(cells[3])),
                "rating": number(text(rating).replace("*", "").strip()),
                "rating_version": version.get("title") if version else default_version,
                "rating_scope": "current tenure on team, not recent form",
            }
        )
    transfers = []
    for row in soup.select('#rosterBox [data-toggle-target="all-time"] .transfer-row'):
        transfers.append(
            {
                "player": entity(row.select_one(".transfer-movement"), "player"),
                "date": timestamp(row.select_one(".transfer-date")),
                "movement": text(row.select_one(".transfer-movement")),
                "teams": [entity(a, "team") for a in row.select('.transfer-movement a[href^="/team/"]')],
            }
        )
    results, upcoming = [], []
    event = None
    for row in soup.select("#matchesBox .event-header-cell, #matchesBox .team-row"):
        if "event-header-cell" in row.get("class", []):
            event = entity(row, "events")
            continue
        teams = [entity(a, "team") for a in row.select("a.team-name")]
        match = entity(row, "matches")
        if not match or len(teams) != 2 or any(t is None for t in teams):
            raise ValueError("Changed HLTV match row")
        scores = [number(text(e)) for e in row.select(".score-cell .score")]
        # Upcoming/live rows use matchpage-button, results use stats-button.
        # A partial LIVE score must not become a completed series in recent form.
        completed = (
            row.select_one("a.stats-button") is not None and len(scores) == 2 and all(s is not None for s in scores)
        )
        record = {
            "id": match["id"],
            "url": match["url"],
            "teams": teams,
            "starts_at": timestamp(row),
            "event": event,
            "scores": scores if completed else None,
            "status": "finished" if completed else "scheduled",
        }
        (results if completed else upcoming).append(record)
    maps = []
    for row in soup.select("#statsBox .map-statistics-container"):
        name_map = text(row.select_one(".map-statistics-row-map-mapname"))
        if not name_map:
            raise ValueError("Changed HLTV map row")
        wdl = {
            text(e.select_one(".description")): number(text(e.select_one(".stat")))
            for e in row.select(".map-statistics-extended-wdl .highlighted-stat")
        }
        history = row.select_one('a[href^="/stats/teams/map/"]')
        filters = parse_qs(urlsplit(history["href"]).query) if history else {}
        maps.append(
            {
                "map": name_map,
                "win_percent": number(text(row.select_one(".map-statistics-row-win-percentage"))),
                "wins": wdl.get("Win"),
                "draws": wdl.get("Draws"),
                "losses": wdl.get("Losses"),
                "period": {k: v[0] for k, v in filters.items() if k in ("startDate", "endDate")},
                "veto": pairs(row, ".map-statistics-extended-highlight-veto"),
                "rounds": pairs(row, ".map-statistics-extended-general-stat"),
                "url": url(history["href"]) if history else address + "#tab-statsBox",
            }
        )
    events = []
    for row in soup.select("#eventsBox a.ongoing-event"):
        event_row = entity(row, "events")
        if event_row:
            events.append(
                event_row
                | {
                    "name": text(row.select_one(".eventbox-eventname")),
                    "dates": [timestamp(e) for e in row.select("[data-unix]")],
                    "placement": text(row.select_one(".placement-cell")) or None,
                }
            )
    achievements = []
    for table in soup.select("#achievementsBox .achievement-table"):
        for row in table.select("tbody tr"):
            event_row = entity(row, "events")
            if event_row:
                achievements.append(
                    {
                        "category": table.parent.get("id"),
                        "event": event_row,
                        "placement": text(row.select_one(".placement-cell")),
                    }
                )
    news = []
    for row in soup.select("#newsBox .subTab-newsArticle"):
        item = entity(row, "news")
        if item:
            title = " ".join(str(child).strip() for child in row.find_all(string=True, recursive=False)).strip()
            news.append({"id": item["id"], "title": title, "date": timestamp(row), "url": item["url"]})
    highlights = {
        text(e.select_one(".description")): {
            "value": text(e.select_one(".stat")),
            "period": text(e.select_one(".stat-timespan")) or None,
        }
        for e in soup.select("#matchesBox .highlighted-stat")
    }
    graphs = []
    # Preserve explicit graph labels/values, not estimates from pixel positions.
    import json

    for node in soup.select("#infoBox [data-chartjs-config]"):
        try:
            graph = json.loads(node["data-chartjs-config"])
            graphs.append({"scope": node.parent.get("class", []), "data": graph.get("data")})
        except (ValueError, TypeError):
            continue
    return {
        "kind": "team",
        "schema_version": VERSION,
        "id": team_id,
        "name": name,
        "url": address,
        "country": country[1] if country else None,
        "rankings": ranks,
        "roster": roster,
        "coach": entity(soup.select_one("#rosterBox .coach-table"), "coach"),
        "transfers": transfers,
        "results": results,
        "upcoming": upcoming,
        "maps": maps,
        "events": events,
        "achievements": achievements,
        "news": news,
        "highlights": highlights,
        "ranking_history": graphs,
        "coverage": {
            "tabs": list(TABS),
            "results": "profile excerpt, not entire history",
            "news": "headlines and dates only",
            "maps": "past 3 months",
        },
    }


def parse_match(html, address):
    soup = soup_for(html)
    found = re.fullmatch(r"/matches/(\d+)/[^/]+", urlsplit(url(address)).path)
    teams = [entity(soup.select_one(f".team{n}-gradient"), "team") for n in (1, 2)]
    clock = soup.select_one(".timeAndEvent")
    if not found or any(t is None for t in teams) or clock is None:
        raise ValueError("Incomplete HLTV match page")
    status_text = text(clock.select_one(".countdown"))
    status = "finished" if status_text == "Match over" else "live" if status_text.upper() == "LIVE" else "scheduled"
    if any(word in status_text.lower() for word in ("postponed", "cancelled", "canceled", "deleted")):
        status = "unavailable"
    maps = []
    for row in soup.select(".mapholder"):
        sides = [row.select_one(f".results-{side}") for side in ("left", "right")]
        maps.append(
            {
                "map": text(row.select_one(".mapname")),
                "teams": [text(e.select_one(".results-teamname")) if e else None for e in sides],
                "scores": [number(text(e.select_one(".results-team-score"))) if e else None for e in sides],
                "picked_by": [
                    text(e.select_one(".results-teamname")) for e in sides if e and "pick" in e.get("class", [])
                ],
                "halves": text(row.select_one(".results-center-half-score")),
            }
        )
    lineups = []
    for row in soup.select("#lineups .lineup"):
        players = {}
        for a in row.select('a[href^="/player/"]'):
            player = entity(a, "player")
            if player and text(a):
                players[player["id"]] = player
        lineups.append({"team": entity(row, "team"), "players": list(players.values())})
    players_stats = []
    for table in soup.select("#match-stats #all-content .totalstats"):
        team = entity(table, "team")
        for row in table.select("tr:not(.header-row)"):
            player = entity(row, "player")
            if player:
                players_stats.append(
                    {
                        "team": team,
                        "player": player,
                        "metrics": {
                            key: text(row.select_one(selector))
                            for key, selector in (
                                ("kd", ".kd.traditional-data"),
                                ("adr", ".adr.traditional-data"),
                                ("kast", ".kast.traditional-data"),
                                ("swing", ".roundSwing"),
                                ("rating", ".rating"),
                            )
                        },
                        "rating_version": text(table.select_one(".header-row .rating")),
                    }
                )
    return {
        "kind": "match",
        "schema_version": VERSION,
        "id": int(found[1]),
        "url": url(address),
        "teams": teams,
        "starts_at": timestamp(clock),
        "event": entity(clock, "events"),
        "status": status,
        "status_text": status_text,
        "veto": [text(e) for e in soup.select(".veto-box")],
        "maps": maps,
        "lineups": lineups,
        "player_stats": players_stats,
    }


def discover_teams(html):
    soup = soup_for(html)
    result = {}
    for node in soup.select('a[href^="/team/"]'):
        team = entity(node, "team")
        if team and team["name"]:
            result[team["id"]] = team
    if not result:
        raise ValueError("No HLTV teams found in discovery page")
    return list(result.values())


def parse_schedule(html):
    soup = soup_for(html)
    result = {}
    for link in soup.select("a.match-teams"):
        match = entity(link, "matches")
        names = [text(e) for e in link.select(".match-teamname")]
        if match and len(names) == 2:
            result[match["id"]] = {
                "id": match["id"],
                "url": match["url"],
                "names": names,
                "starts_at": timestamp(link.parent),
                "live": link.parent.select_one(".match-meta-live") is not None,
            }
    if not result:
        raise ValueError("HLTV match directory is empty or changed")
    return list(result.values())
