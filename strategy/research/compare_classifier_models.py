"""Compares classifier models on real articles, for cost against agreement.

The news classifier runs on gpt-5.5, which at measured volume costs roughly
$40 a month. Classifying a headline into sentiment and affected assets is
not obviously a frontier-model task, so the question is whether a cheaper
model produces the same answers.

Uses the production prompt and real stored articles, because a test on
synthetic inputs would measure the wrong thing. Reports per-model token
usage and cost, and agreement with the incumbent on the parts the strategy
actually consumes: which assets were flagged, and in which direction.

Nothing here writes to the pipeline. It is a read-only comparison.

Usage:
    .venv/bin/python3 -m strategy.research.compare_classifier_models \\
        --sample 25 --models gpt-5.4-mini,gpt-5.4-nano
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from data_collector.news_data.collectors.news_sentiment import (  # noqa: E402
    ALL_ASSETS,
    PROMPT_TEMPLATE,
)


# Per million tokens. Passed in rather than hardcoded, since prices move and
# a stale constant produces a confident wrong answer.
DEFAULT_PRICES = {
    "gpt-5.5": (5.00, 30.00),
}


def load_articles(database_url: str, limit: int) -> list[tuple[str, str]]:
    connection = psycopg2.connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT title, COALESCE(summary, '')
                FROM news_articles
                WHERE title IS NOT NULL AND length(title) > 20
                ORDER BY published DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            return [(str(r[0]), str(r[1])) for r in cursor.fetchall()]
    finally:
        connection.close()


def classify(
    client: OpenAI, model: str, title: str, summary: str
) -> tuple[dict, int, int]:
    prompt = PROMPT_TEMPLATE.format(
        assets=", ".join(ALL_ASSETS), title=title, summary=summary
    )
    response = client.responses.create(model=model, input=prompt)
    usage = getattr(response, "usage", None)
    text = response.output_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = {"impacts": [], "_unparsed": text[:200]}
    return (
        parsed,
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def impact_set(result: dict) -> set[tuple[str, str]]:
    """Asset and direction pairs, which is what the strategy consumes.

    Sentiment magnitude is deliberately excluded: small numeric differences
    between models are expected and do not change whether a signal fires.
    """
    out: set[tuple[str, str]] = set()
    for impact in result.get("impacts") or []:
        asset = str(impact.get("asset", "")).strip()
        direction = str(impact.get("direction", "")).strip().lower()
        if asset:
            out.add((asset, direction))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare classifier models on cost and agreement."
    )
    parser.add_argument("--sample", type=int, default=25)
    parser.add_argument("--baseline", default="gpt-5.5")
    parser.add_argument("--models", default="gpt-5.4-mini,gpt-5.4-nano")
    parser.add_argument(
        "--prices",
        default="",
        help="model:in,out;model:in,out per million tokens.",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set.")

    prices = dict(DEFAULT_PRICES)
    for entry in filter(None, args.prices.split(";")):
        name, values = entry.split(":")
        first, second = values.split(",")
        prices[name.strip()] = (float(first), float(second))

    articles = load_articles(database_url, args.sample)
    if not articles:
        raise SystemExit("No articles to test against.")

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    models = [args.baseline] + [
        m.strip() for m in args.models.split(",") if m.strip()
    ]

    results: dict[str, list] = {m: [] for m in models}
    tokens: dict[str, list[int]] = {m: [0, 0] for m in models}
    failures: dict[str, int] = {m: 0 for m in models}

    print(f"\nClassifying {len(articles)} real articles with {len(models)} models\n")
    for index, (title, summary) in enumerate(articles, 1):
        for model in models:
            try:
                parsed, tin, tout = classify(client, model, title, summary)
                results[model].append(parsed)
                tokens[model][0] += tin
                tokens[model][1] += tout
            except Exception as error:  # noqa: BLE001
                failures[model] += 1
                results[model].append({"impacts": [], "_error": str(error)[:80]})
            time.sleep(0.2)
        if index % 5 == 0:
            print(f"  {index}/{len(articles)}...", flush=True)

    baseline_results = results[args.baseline]

    print(f"\n{'model':16} {'in/call':>8} {'out/call':>9} "
          f"{'$/1k calls':>11} {'agree':>7} {'exact':>7} {'fails':>6}")
    print("-" * 70)

    monthly_calls = 284 * 30
    for model in models:
        n = len(results[model])
        avg_in = tokens[model][0] / n if n else 0
        avg_out = tokens[model][1] / n if n else 0
        price = prices.get(model)
        if price:
            per_call = avg_in / 1e6 * price[0] + avg_out / 1e6 * price[1]
            cost = f"${per_call * 1000:10.2f}"
            monthly = per_call * monthly_calls
        else:
            cost = "  unpriced"
            monthly = None

        if model == args.baseline:
            agree = exact = 100.0
        else:
            overlaps = []
            exacts = 0
            for mine, theirs in zip(results[model], baseline_results):
                a, b = impact_set(mine), impact_set(theirs)
                if a == b:
                    exacts += 1
                union = a | b
                overlaps.append(len(a & b) / len(union) if union else 1.0)
            agree = 100.0 * sum(overlaps) / len(overlaps)
            exact = 100.0 * exacts / len(baseline_results)

        print(
            f"{model:16} {avg_in:8.0f} {avg_out:9.0f} {cost} "
            f"{agree:6.1f}% {exact:6.1f}% {failures[model]:6}"
        )
        if monthly is not None:
            print(f"{'':16} projected monthly at 284 calls/day: ${monthly:,.2f}")

    print(
        "\nagree = average overlap of (asset, direction) pairs against the "
        "baseline.\nexact = share of articles where the two agreed "
        "completely.\nSentiment magnitude is excluded: small numeric "
        "differences do not change\nwhether a signal fires."
    )


if __name__ == "__main__":
    main()
