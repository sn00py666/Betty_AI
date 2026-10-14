"""Сравнение промптов на вымышленных фактах: python scripts/compare_forecast_prompts.py.

Результаты сохраняются локально; запросы учитываются для статистики без дневной квоты.
Один запуск: пять сценариев × число температур × число повторов.
"""

import argparse
import asyncio
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import Settings  # noqa: E402
from services.ai import Commentary, request_body  # noqa: E402
from services.users import Users  # noqa: E402


def scenarios():
    base = dict(
        sport="cs2",
        team1="Aurora",
        team2="BB Team",
        tournament="Test Cup",
        status="prematch",
        best_of=3,
    )
    equal = dict(games=5, wins=3)
    cases = {
        "equal_sparse": dict(team1=equal, team2=equal, head_to_head=dict(games=1, team1_wins=1)),
        "recent_form": dict(team1=dict(games=5, wins=4), team2=dict(games=5, wins=1)),
        "small_tournament_edge": dict(
            team1=equal | dict(tournament_games=4, tournament_wins=3),
            team2=equal | dict(tournament_games=3, tournament_wins=2),
            head_to_head=dict(games=2, team1_wins=1),
        ),
        "rich": dict(
            team1=equal
            | dict(
                top10=dict(games=5, wins=3),
                winning_round_margin=3,
                active_maps=["Ancient", "Mirage", "Nuke"],
                tournament_games=2,
                tournament_wins=1,
            ),
            team2=equal
            | dict(
                top10=dict(games=5, wins=2),
                winning_round_margin=1,
                active_maps=["Ancient", "Mirage"],
                tournament_games=2,
                tournament_wins=1,
            ),
            head_to_head=dict(games=1, team1_wins=1),
        ),
        "conflicting": dict(
            team1=dict(games=5, wins=4, tournament_games=4, tournament_wins=1),
            team2=dict(games=5, wins=2, tournament_games=4, tournament_wins=3),
        ),
    }
    return {name: deepcopy(base | dict(statistics=stats)) for name, stats in cases.items()}


async def run(args):
    settings = Settings()
    if not settings.ai_api_key or not settings.ai_api_key.get_secret_value():
        raise SystemExit("AI_API_KEY не настроен")
    variants = [(f"candidate_{t:g}", t, None) for t in args.temperatures]
    if args.baseline:
        baseline = await asyncio.to_thread(Path(args.baseline).read_text)
        variants.insert(0, ("baseline_0.2", 0.2, baseline))
    usage = Users(ROOT / "data" / "ai_usage.json")
    today = datetime.now(UTC).date().isoformat()
    previous = usage.get(0)
    count = previous.get("count", 0) if previous.get("date") == today else 0
    results = []
    # Последовательно: счётчик сохраняется перед каждой попыткой, включая ошибки.
    async with httpx.AsyncClient(timeout=45) as client:
        for variant, temperature, prompt in variants:
            for name, match in scenarios().items():
                for repeat in range(args.repeats):
                    body = request_body(settings.ai_model, match, temperature)
                    if prompt is not None:
                        body["messages"][0]["content"] = prompt
                        payload = json.loads(body["messages"][1]["content"])
                        payload.pop("recent_form", None)
                        for stats in payload["statistics"].values():
                            stats.pop("recent", None)
                        body["messages"][1]["content"] = json.dumps(payload, ensure_ascii=False)
                    count += 1
                    usage.update(0, date=today, count=count)
                    row = dict(variant=variant, scenario=name, repeat=repeat + 1)
                    try:
                        response = await client.post(
                            settings.ai_base_url.rstrip("/") + "/chat/completions",
                            json=body,
                            headers={"Authorization": "Bearer " + settings.ai_api_key.get_secret_value()},
                        )
                        response.raise_for_status()
                        payload = response.json()
                        answer = Commentary.model_validate_json(payload["choices"][0]["message"]["content"])
                        row.update(answer=answer.model_dump(), usage=payload.get("usage"))
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
                        row["error"] = type(error).__name__
                    results.append(row)
                    output = Path(args.output)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(
                        output.write_text,
                        json.dumps(
                            dict(
                                model=settings.ai_model,
                                synthetic=True,
                                created_at=datetime.now(UTC).isoformat(),
                                prompts={
                                    label: p or request_body(settings.ai_model, match)["messages"][0]["content"]
                                    for label, _, p in variants
                                },
                                scenarios=scenarios(),
                                results=results,
                            ),
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    if "error" in row:
                        raise SystemExit("Проверка остановлена после ошибки провайдера; результаты сохранены")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temperatures", nargs="+", type=float, choices=None, default=[0.35, 0.65])
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--baseline", help="Файл с прежним промптом для контрольного варианта")
    parser.add_argument("--output", default=str(ROOT / "data" / "prompt_experiments.json"))
    options = parser.parse_args()
    if not options.temperatures or any(not 0 <= value <= 2 for value in options.temperatures):
        parser.error("Температура должна быть от 0 до 2")
    asyncio.run(run(options))
