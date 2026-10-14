# HLTV fixtures

Public pages captured on 2026-10-15 with local Google Chrome / Playwright:

- `team.html`: https://www.hltv.org/team/7020/spirit
- `aurora.html`: https://www.hltv.org/team/11861/aurora
- `match.html`: https://www.hltv.org/matches/2398756/aurora-vs-spirit-esl-pro-league-season-24
- `matches.html`: https://www.hltv.org/matches

Reduced to parser-relevant containers. Scripts, ads, comments, image URLs, styles,
most news/event/achievement/transfer rows, map rows after the first two and ranking
chart datasets were removed. The match fixture retains per-map/side tables and
ordinary/eco-adjusted columns to test that the parser selects only the correct totals.
The schedule keeps five cards. Values and retained class names come from the real DOM.
No cookies, account state or credentials are included. Tests make no network requests.

`news.html`: сокращённый редакционный DOM статьи
https://www.hltv.org/news/45699/woxic-on-taking-bronze-its-always-good-to-at-least-have-something-from-the-event
получен обычным Chrome по HTTPS 15 октября 2026 года. Изображения, iframe и tooltip
удалены; абзацы, дата, автор и ссылки сохранены для регрессионной проверки.
Контрактные примеры результатов/статистики в test_hltv_archive.py являются
синтетическими: соответствующие исследованные страницы возвращали HTTP 403.
