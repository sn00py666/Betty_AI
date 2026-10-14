"""Archive pages: source-labelled documents, articles and paginated results.

Keep document tables and headings instead of guessing undocumented metric meanings.
Only explicit HLTV links are followed; UI/sidebar/forum content is excluded.
"""

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from integrations.hltv import VERSION, entity, number, soup_for, text, timestamp, url

PATTERNS = {
    "news": r"/news/\d+/[^/]+",
    "player": r"/player/\d+/[^/]+",
    "event": r"/events/\d+/[^/]+",
    "stats": r"/stats/(?:teams|players)(?:/[a-z]+)?/\d+/[^/]+",
    "mapstats": r"/stats/matches/(?:mapstatsid|performance/mapstatsid|economy/mapstatsid)/\d+/[^/]+",
    "match": r"/matches/\d+/[^/]+",
    "team": r"/team/\d+/[^/]+",
}


def archive_url(value):
    """Canonical, finite URL families; never follow arbitrary query permutations."""
    parsed = urlsplit(url(value))
    query = parse_qs(parsed.query)
    if parsed.path == "/results":
        if set(query) - {"offset", "team"} or any(len(v) != 1 or not v[0].isdigit() for v in query.values()):
            raise ValueError("Unsupported HLTV result filters")
        query = {k: v[0] for k, v in sorted(query.items()) if not (k == "offset" and v == ["0"])}
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
    if parsed.query or not any(re.fullmatch(pattern, parsed.path) for pattern in PATTERNS.values()):
        raise ValueError("Unsupported HLTV archive URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def page_kind(address):
    path = urlsplit(archive_url(address)).path
    if path == "/results":
        return "results"
    return next(kind for kind, pattern in PATTERNS.items() if re.fullmatch(pattern, path))


def links(root):
    found = {}
    for link in root.select("a[href]"):
        try:
            address = archive_url(link["href"])
        except ValueError:
            continue
        found[address] = {"url": address, "kind": page_kind(address), "label": text(link)}
    return list(found.values())


def parse_article(html, address):
    soup = soup_for(html)
    article = soup.select_one("article.newsitem")
    body = article.select_one(".newstext-con") if article else None
    title = text(article.select_one("h1.headline")) if article else ""
    published = timestamp(article.select_one(".article-info")) if article else None
    if not body or not title or not published:
        raise ValueError("Incomplete HLTV article")
    for node in body.select(".tooltip-con, .featured-quote, .image-con, .audioCon, .video-con"):
        node.decompose()
    paragraphs = [text(p) for p in body.select("p, h2, h3, li") if text(p)]
    if not paragraphs:
        raise ValueError("HLTV article body missing")
    return {
        "kind": "news",
        "schema_version": VERSION,
        "url": archive_url(address),
        "id": int(urlsplit(address).path.split("/")[2]),
        "title": title,
        "published_at": published,
        "author": text(article.select_one(".authorName")),
        "paragraphs": paragraphs,
        "links": links(body),
        "content_scope": "editorial article, no comments",
    }


def parse_results(html, address):
    soup = soup_for(html)
    root = soup.select_one(".results-all")
    if root is None:
        raise ValueError("HLTV result directory missing")
    rows = {}
    for row in root.select(".result-con"):
        match = entity(row, "matches")
        if not match:
            raise ValueError("HLTV result without match ID")
        # Result lists may expose names only. Resolve exact IDs on the match page.
        names = [text(row.select_one(".team1 .team")), text(row.select_one(".team2 .team"))]
        score = row.select_one(".result-score")
        scores = [number(text(s)) for s in score.select("span")] if score else []
        if len(scores) != 2 or None in scores or not all(names):
            raise ValueError("Incomplete HLTV result row")
        rows[match["id"]] = dict(
            id=match["id"],
            url=match["url"],
            names=names,
            scores=scores,
            starts_at=timestamp(row),
            status="finished",
        )
    current = parse_qs(urlsplit(address).query)
    offset = int(current.get("offset", ["0"])[0])
    next_pages = []
    for link in soup.select('a[href*="offset="]'):
        try:
            candidate = archive_url(link["href"])
        except ValueError:
            continue
        parsed = urlsplit(candidate)
        query = parse_qs(parsed.query)
        if parsed.path == "/results" and query.get("team") == current.get("team"):
            if int(query.get("offset", ["0"])[0]) > offset:
                next_pages.append(candidate)
    if not rows and not re.search(r"no results|no matches", text(root), re.I):
        raise ValueError("Empty or changed HLTV result directory")
    next_page = min(next_pages, key=lambda u: int(parse_qs(urlsplit(u).query)["offset"][0])) if next_pages else None
    return dict(
        kind="results",
        schema_version=VERSION,
        url=archive_url(address),
        results=list(rows.values()),
        next_page=next_page,
        complete=not next_page,
        links=[{"url": r["url"], "kind": "match"} for r in rows.values()],
    )


def parse_document(html, address):
    """Preserve labelled source tables/sections for players, events and map statistics."""
    kind = page_kind(address)
    if kind not in {"player", "event", "stats", "mapstats"}:
        raise ValueError("Unsupported HLTV document kind")
    soup = soup_for(html)
    root = soup.select_one(".contentCol")
    title = text(soup.title).removesuffix(" | HLTV.org")
    if root is None or not title or "just a moment" in title.lower():
        raise ValueError("Incomplete HLTV document")
    for node in root.select(
        ".forum, .forum-container, #comments, .comments, .tooltip-con, .betting, .bookmaker, "
        '.ad, .ads, .newsdsl, .ai-briefing, [class*="aiBriefing"]'
    ):
        node.decompose()
    tables = []
    for table in root.select("table"):
        rows = [[text(c) for c in row.find_all(["th", "td"], recursive=False)] for row in table.select("tr")]
        tables.append({"classes": table.get("class", []), "rows": [r for r in rows if r]})
    document_text = root.get_text("\n", strip=True)
    if len(document_text) < 80:
        raise ValueError("Empty HLTV document")
    return dict(
        kind=kind,
        schema_version=VERSION,
        url=archive_url(address),
        title=title,
        headings=[text(e) for e in root.select("h1,h2,h3,.headline,.section-header")],
        text=document_text,
        tables=tables,
        links=links(root),
        coverage="source-labelled document; periods and rating versions retained as published",
    )


def parse_archive(html, address):
    kind = page_kind(address)
    if kind == "news":
        return parse_article(html, address)
    if kind == "results":
        return parse_results(html, address)
    return parse_document(html, address)
