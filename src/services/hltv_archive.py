"""Persistent crawl frontier and indexed series history. No browser or LLM calls here."""

import json
import sqlite3
from contextlib import closing, contextmanager
from urllib.parse import parse_qs, urlsplit

from integrations.hltv_archive import archive_url, page_kind


class Archive:
    def __init__(self, directory):
        self.path = directory / "archive.sqlite3"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    url TEXT PRIMARY KEY, kind TEXT NOT NULL, priority INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', due REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT, updated REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(status, due, priority);
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY, starts_at TEXT NOT NULL, team1 INTEGER NOT NULL,
                    team2 INTEGER NOT NULL, fetched_at REAL NOT NULL, body TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS results_team1 ON results(team1, starts_at);
                CREATE INDEX IF NOT EXISTS results_team2 ON results(team2, starts_at);
            """)
            yield db

    def enqueue(self, addresses, priority=50, *, refresh_after=None, now=None):
        rows = []
        for address in addresses:
            try:
                address = archive_url(address)
                rows.append((address, page_kind(address), priority))
            except ValueError:
                continue
        with self.connect() as db:
            db.executemany(
                """INSERT INTO jobs(url,kind,priority) VALUES(?,?,?)
                ON CONFLICT(url) DO UPDATE SET priority=MIN(priority,excluded.priority)""",
                rows,
            )
            if refresh_after is not None and now is not None:
                db.executemany(
                    "UPDATE jobs SET status='queued',due=0 WHERE url=? AND status='done' AND updated<=?",
                    [(r[0], now - refresh_after) for r in rows],
                )

    def next(self, now):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','retry') AND due<=? ORDER BY priority,due,url LIMIT 1",
                (now,),
            ).fetchone()
            return dict(row) if row else None

    def finish(self, address, now, *, error=None, blocked=False):
        with self.connect() as db:
            row = db.execute("SELECT attempts,status FROM jobs WHERE url=?", (address,)).fetchone()
            if blocked and row and row["status"] == "blocked":
                return
            attempts = (row["attempts"] if row else 0) + 1
            status = "blocked" if blocked else "retry" if error else "done"
            delay = min(86400, 300 * 2 ** min(attempts, 8)) if error else 0
            db.execute(
                "UPDATE jobs SET status=?,due=?,attempts=?,error=?,updated=? WHERE url=?",
                (status, now + delay, attempts, error, now, address),
            )

    def counts(self):
        with self.connect() as db:
            return {r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}

    def record_results(self, records, fetched_at):
        rows = []
        for record in records:
            teams, scores = record.get("teams", []), record.get("scores")
            if (
                record.get("status") != "finished"
                or len(teams) != 2
                or not record.get("starts_at")
                or not scores
                or len(scores) != 2
                or None in scores
            ):
                continue
            rows.append(
                (
                    record["id"],
                    record["starts_at"],
                    teams[0]["id"],
                    teams[1]["id"],
                    fetched_at,
                    json.dumps(record, ensure_ascii=False),
                )
            )
        with self.connect() as db:
            db.executemany(
                "INSERT INTO results VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "starts_at=excluded.starts_at,team1=excluded.team1,team2=excluded.team2,"
                "fetched_at=excluded.fetched_at,body=excluded.body",
                rows,
            )

    def results(self, team_id, since, before, known_by):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            db.row_factory = sqlite3.Row
            return [
                json.loads(r["body"])
                for r in db.execute(
                    "SELECT body FROM results WHERE (team1=? OR team2=?) AND starts_at>=? AND starts_at<? "
                    "AND fetched_at<=? ORDER BY starts_at DESC",
                    (team_id, team_id, since, before, known_by),
                )
            ]

    def discover(self, data, now):
        kind = data["kind"]
        if kind == "team":
            self.record_results(data["results"], now)
            self.enqueue(["https://www.hltv.org/results?team=" + str(data["id"])], 10, refresh_after=21600, now=now)
            self.enqueue([data["url"].replace("/team/", "/stats/teams/")], 20, refresh_after=86400, now=now)
            self.enqueue([p["url"] for p in data["roster"]], 20, refresh_after=86400, now=now)
            self.enqueue(
                [p["url"].replace("/player/", "/stats/players/") for p in data["roster"]],
                20,
                refresh_after=86400,
                now=now,
            )
            self.enqueue([p["url"] for p in data.get("news", [])], 70)
            self.enqueue([p["url"] for p in data.get("news", [])[:20]], 20)
            self.enqueue([p["url"] for p in data["results"] + data["upcoming"]], 30)
            self.enqueue(
                [p["event"]["url"] for p in data["upcoming"] if p.get("event")], 20, refresh_after=21600, now=now
            )
            self.enqueue([e["url"] for e in data["events"]], 30)
            self.enqueue([e["event"]["url"] for e in data["achievements"]], 70)
        elif kind == "match":
            self.record_results([data], now)
            self.enqueue([t["url"] for t in data["teams"]], 80)
            self.enqueue([data["event"]["url"]] if data.get("event") else [], 60)
            self.enqueue(data.get("mapstats_urls", []), 60)
            self.enqueue([p["url"] for row in data["lineups"] for p in row["players"]], 60)
        else:
            self.enqueue([link["url"] for link in data.get("links", [])], 70)
        if kind == "results" and data.get("next_page"):
            # Complete pagination independently of the number of match-detail pages.
            query = parse_qs(urlsplit(data["url"]).query)
            self.enqueue([data["next_page"]], 40 if "team" in query else 75)
