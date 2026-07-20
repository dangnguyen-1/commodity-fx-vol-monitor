from __future__ import annotations

import math
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    raise ImportError(
        "PyYAML is required. Install it with: "
        "python3 -m pip install PyYAML"
    ) from exc


PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_SPEC_PATH = (
    PROJECT_ROOT
    / "strategy"
    / "config"
    / "intraday"
    / "intraday_strategy_spec.yaml"
)


REQUIRED_TOP_LEVEL_SECTIONS = [
    "strategy",
    "capital",
    "data",
    "relationships",
    "features",
    "signals",
    "execution",
    "position_sizing",
    "risk",
    "exits",
    "sessions",
    "paper_ledger",
    "monitoring",
    "dashboard",
    "governance",
]


EXPECTED_NEWS_SOURCES = {
    "Reuters",
    "MarketWatch",
    "Investing.com",
}


class IntradaySpecError(ValueError):
    """Raised when the intraday specification is invalid."""


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_mapping(
    value: Any,
    path: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntradaySpecError(
            f"{path} must be a mapping."
        )

    return value


def _require_list(
    value: Any,
    path: str,
) -> list[Any]:
    if not isinstance(value, list):
        raise IntradaySpecError(
            f"{path} must be a list."
        )

    return value


def _require_string(
    value: Any,
    path: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
    ):
        raise IntradaySpecError(
            f"{path} must be a non-empty string."
        )

    return value.strip()


def _require_bool(
    value: Any,
    path: str,
) -> bool:
    if not isinstance(value, bool):
        raise IntradaySpecError(
            f"{path} must be true or false."
        )

    return value


def _require_number(
    value: Any,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if not _is_number(value):
        raise IntradaySpecError(
            f"{path} must be a finite number."
        )

    result = float(value)

    if (
        minimum is not None
        and result < minimum
    ):
        raise IntradaySpecError(
            f"{path} must be at least {minimum}."
        )

    if (
        maximum is not None
        and result > maximum
    ):
        raise IntradaySpecError(
            f"{path} must be at most {maximum}."
        )

    return result


def _require_int(
    value: Any,
    path: str,
    *,
    minimum: int | None = None,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
    ):
        raise IntradaySpecError(
            f"{path} must be an integer."
        )

    if (
        minimum is not None
        and value < minimum
    ):
        raise IntradaySpecError(
            f"{path} must be at least {minimum}."
        )

    return value


def _require_keys(
    mapping: dict[str, Any],
    required_keys: list[str],
    path: str,
) -> None:
    missing = sorted(
        set(required_keys)
        - set(mapping)
    )

    if missing:
        raise IntradaySpecError(
            f"{path} is missing required keys: "
            f"{missing}"
        )


def _parse_minute_interval(
    value: Any,
    path: str,
) -> int:
    interval = _require_string(
        value,
        path,
    )

    match = re.fullmatch(
        r"([1-9][0-9]*)m",
        interval,
    )

    if match is None:
        raise IntradaySpecError(
            f"{path} must use minute format such "
            "as '1m' or '5m'."
        )

    return int(match.group(1))


def _resolve_project_path(
    value: Any,
    path: str,
) -> Path:
    raw_path = Path(
        _require_string(
            value,
            path,
        )
    )

    if raw_path.is_absolute():
        resolved = raw_path
    else:
        resolved = (
            PROJECT_ROOT
            / raw_path
        )

    return resolved.resolve()


def _validate_strategy(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["strategy"],
        "strategy",
    )

    _require_keys(
        section,
        [
            "name",
            "specification_version",
            "status",
            "parent_research_version",
            "paper_trading_only",
            "live_capital_approved",
        ],
        "strategy",
    )

    _require_string(
        section["name"],
        "strategy.name",
    )

    _require_string(
        section["specification_version"],
        "strategy.specification_version",
    )

    _require_string(
        section["status"],
        "strategy.status",
    )

    _require_string(
        section["parent_research_version"],
        "strategy.parent_research_version",
    )

    paper_only = _require_bool(
        section["paper_trading_only"],
        "strategy.paper_trading_only",
    )

    live_approved = _require_bool(
        section["live_capital_approved"],
        "strategy.live_capital_approved",
    )

    if not paper_only:
        raise IntradaySpecError(
            "The intraday candidate must remain "
            "paper-trading-only."
        )

    if live_approved:
        raise IntradaySpecError(
            "The intraday candidate cannot be "
            "approved for live capital."
        )


def _validate_capital(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["capital"],
        "capital",
    )

    _require_number(
        section.get(
            "initial_equity_usd"
        ),
        "capital.initial_equity_usd",
        minimum=1.0,
    )

    base_currency = _require_string(
        section.get("base_currency"),
        "capital.base_currency",
    )

    if base_currency != "USD":
        raise IntradaySpecError(
            "capital.base_currency must currently "
            "be USD."
        )


def _validate_market_data(
    spec: dict[str, Any],
) -> None:
    data = _require_mapping(
        spec["data"],
        "data",
    )

    market = _require_mapping(
        data.get("market"),
        "data.market",
    )

    input_minutes = _parse_minute_interval(
        market.get(
            "input_bar_interval"
        ),
        "data.market.input_bar_interval",
    )

    evaluation_minutes = (
        _parse_minute_interval(
            market.get(
                "signal_evaluation_interval"
            ),
            (
                "data.market."
                "signal_evaluation_interval"
            ),
        )
    )

    monitoring_minutes = (
        _parse_minute_interval(
            market.get(
                "position_monitoring_interval"
            ),
            (
                "data.market."
                "position_monitoring_interval"
            ),
        )
    )

    if input_minutes != 1:
        raise IntradaySpecError(
            "The current engine supports only "
            "1-minute input bars."
        )

    if (
        evaluation_minutes
        % input_minutes
        != 0
    ):
        raise IntradaySpecError(
            "Signal evaluation interval must be "
            "a multiple of the input interval."
        )

    if (
        monitoring_minutes
        % input_minutes
        != 0
    ):
        raise IntradaySpecError(
            "Position monitoring interval must be "
            "a multiple of the input interval."
        )

    _require_bool(
        market.get(
            "require_completed_bars"
        ),
        (
            "data.market."
            "require_completed_bars"
        ),
    )

    _require_int(
        market.get(
            "maximum_bar_lateness_seconds"
        ),
        (
            "data.market."
            "maximum_bar_lateness_seconds"
        ),
        minimum=0,
    )

    _require_number(
        market.get(
            "minimum_window_coverage_pct"
        ),
        (
            "data.market."
            "minimum_window_coverage_pct"
        ),
        minimum=0.0,
        maximum=100.0,
    )

    timestamp_standard = (
        _require_string(
            market.get(
                "timestamp_standard"
            ),
            (
                "data.market."
                "timestamp_standard"
            ),
        )
    )

    if timestamp_standard != "UTC":
        raise IntradaySpecError(
            "All stored timestamps must use UTC."
        )


def _validate_news(
    spec: dict[str, Any],
) -> None:
    data = _require_mapping(
        spec["data"],
        "data",
    )

    news = _require_mapping(
        data.get("news"),
        "data.news",
    )

    sources = _require_list(
        news.get("sources"),
        "data.news.sources",
    )

    normalized_sources = {
        _require_string(
            source,
            "data.news.sources[]",
        )
        for source in sources
    }

    if normalized_sources != EXPECTED_NEWS_SOURCES:
        raise IntradaySpecError(
            "data.news.sources must contain exactly: "
            "Reuters, MarketWatch, and Investing.com."
        )

    if len(sources) != len(
        normalized_sources
    ):
        raise IntradaySpecError(
            "data.news.sources contains duplicates."
        )

    _require_int(
        news.get(
            "polling_interval_seconds"
        ),
        (
            "data.news."
            "polling_interval_seconds"
        ),
        minimum=1,
    )

    required_fields = _require_list(
        news.get("required_fields"),
        "data.news.required_fields",
    )

    if not required_fields:
        raise IntradaySpecError(
            "data.news.required_fields cannot "
            "be empty."
        )

    deduplication = _require_mapping(
        news.get("deduplication"),
        "data.news.deduplication",
    )

    _require_string(
        deduplication.get("method"),
        "data.news.deduplication.method",
    )

    _require_int(
        deduplication.get(
            "lookback_hours"
        ),
        (
            "data.news.deduplication."
            "lookback_hours"
        ),
        minimum=1,
    )

    sentiment = _require_mapping(
        news.get(
            "sentiment_classification"
        ),
        (
            "data.news."
            "sentiment_classification"
        ),
    )

    provider = _require_string(
        sentiment.get("provider"),
        (
            "data.news."
            "sentiment_classification."
            "provider"
        ),
    )

    if provider != "openai":
        raise IntradaySpecError(
            "The current sentiment provider must "
            "be openai."
        )

    _require_string(
        sentiment.get(
            "model_environment_variable"
        ),
        (
            "data.news."
            "sentiment_classification."
            "model_environment_variable"
        ),
    )

    sentiment_range = _require_mapping(
        sentiment.get(
            "sentiment_range"
        ),
        (
            "data.news."
            "sentiment_classification."
            "sentiment_range"
        ),
    )

    minimum = _require_number(
        sentiment_range.get("minimum"),
        (
            "data.news."
            "sentiment_classification."
            "sentiment_range.minimum"
        ),
    )

    maximum = _require_number(
        sentiment_range.get("maximum"),
        (
            "data.news."
            "sentiment_classification."
            "sentiment_range.maximum"
        ),
    )

    if minimum >= maximum:
        raise IntradaySpecError(
            "Sentiment minimum must be below "
            "sentiment maximum."
        )

    _require_number(
        sentiment.get(
            "minimum_relevance_confidence"
        ),
        (
            "data.news."
            "sentiment_classification."
            "minimum_relevance_confidence"
        ),
        minimum=0.0,
        maximum=1.0,
    )


def _validate_relationships(
    spec: dict[str, Any],
) -> Path:
    section = _require_mapping(
        spec["relationships"],
        "relationships",
    )

    metadata_path = (
        _resolve_project_path(
            section.get(
                "metadata_source"
            ),
            (
                "relationships."
                "metadata_source"
            ),
        )
    )

    if not metadata_path.exists():
        raise IntradaySpecError(
            "Relationship metadata does not exist: "
            f"{metadata_path}"
        )

    qualified_weight = _require_number(
        section.get(
            "qualified_relationship_weight"
        ),
        (
            "relationships."
            "qualified_relationship_weight"
        ),
        minimum=0.0,
        maximum=1.0,
    )

    weak_weight = _require_number(
        section.get(
            "weak_relationship_weight"
        ),
        (
            "relationships."
            "weak_relationship_weight"
        ),
        minimum=0.0,
        maximum=1.0,
    )

    if weak_weight > qualified_weight:
        raise IntradaySpecError(
            "Weak relationship weight cannot "
            "exceed qualified relationship weight."
        )

    return metadata_path


def _validate_features(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["features"],
        "features",
    )

    windows = _require_list(
        section.get(
            "commodity_return_windows_minutes"
        ),
        (
            "features."
            "commodity_return_windows_minutes"
        ),
    )

    parsed_windows = [
        _require_int(
            window,
            (
                "features."
                "commodity_return_windows_minutes[]"
            ),
            minimum=1,
        )
        for window in windows
    ]

    if len(parsed_windows) != len(
        set(parsed_windows)
    ):
        raise IntradaySpecError(
            "Commodity return windows must be "
            "unique."
        )

    if parsed_windows != sorted(
        parsed_windows
    ):
        raise IntradaySpecError(
            "Commodity return windows must be "
            "sorted in ascending order."
        )

    _require_int(
        section.get(
            "fx_return_window_minutes"
        ),
        (
            "features."
            "fx_return_window_minutes"
        ),
        minimum=1,
    )

    _require_int(
        section.get(
            "realized_volatility_window_minutes"
        ),
        (
            "features."
            "realized_volatility_window_minutes"
        ),
        minimum=2,
    )

    weights = _require_mapping(
        section.get(
            "market_impulse_weights"
        ),
        (
            "features."
            "market_impulse_weights"
        ),
    )

    weight_values = [
        _require_number(
            value,
            (
                "features."
                "market_impulse_weights."
                f"{key}"
            ),
            minimum=0.0,
            maximum=1.0,
        )
        for key, value in weights.items()
    ]

    if not math.isclose(
        sum(weight_values),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise IntradaySpecError(
            "Market impulse weights must sum "
            "to exactly 1.0."
        )

    normalization = _require_mapping(
        section.get("normalization"),
        "features.normalization",
    )

    _require_number(
        normalization.get(
            "minimum_volatility_floor"
        ),
        (
            "features.normalization."
            "minimum_volatility_floor"
        ),
        minimum=0.0,
    )

    news = _require_mapping(
        section.get("news"),
        "features.news",
    )

    full_weight = _require_int(
        news.get(
            "full_weight_minutes"
        ),
        "features.news.full_weight_minutes",
        minimum=0,
    )

    half_life = _require_int(
        news.get(
            "decay_half_life_minutes"
        ),
        (
            "features.news."
            "decay_half_life_minutes"
        ),
        minimum=1,
    )

    expiry = _require_int(
        news.get(
            "expiry_minutes"
        ),
        "features.news.expiry_minutes",
        minimum=1,
    )

    if expiry <= full_weight:
        raise IntradaySpecError(
            "News expiry must occur after the "
            "full-weight period."
        )

    if half_life > expiry:
        raise IntradaySpecError(
            "News decay half-life cannot exceed "
            "the expiry window."
        )


def _validate_signals(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["signals"],
        "signals",
    )

    evaluation = _require_mapping(
        section.get("evaluation"),
        "signals.evaluation",
    )

    interval = _require_int(
        evaluation.get(
            "interval_minutes"
        ),
        (
            "signals.evaluation."
            "interval_minutes"
        ),
        minimum=1,
    )

    data_interval = (
        _parse_minute_interval(
            spec["data"]["market"][
                "signal_evaluation_interval"
            ],
            (
                "data.market."
                "signal_evaluation_interval"
            ),
        )
    )

    if interval != data_interval:
        raise IntradaySpecError(
            "Signal evaluation intervals disagree "
            "between data.market and signals."
        )

    evaluation_minutes = _require_list(
        evaluation.get(
            "evaluate_on_minutes"
        ),
        (
            "signals.evaluation."
            "evaluate_on_minutes"
        ),
    )

    parsed_minutes = [
        _require_int(
            minute,
            (
                "signals.evaluation."
                "evaluate_on_minutes[]"
            ),
            minimum=0,
        )
        for minute in evaluation_minutes
    ]

    if any(
        minute > 59
        for minute in parsed_minutes
    ):
        raise IntradaySpecError(
            "Evaluation minutes must be between "
            "0 and 59."
        )

    expected_minutes = list(
        range(
            0,
            60,
            interval,
        )
    )

    if parsed_minutes != expected_minutes:
        raise IntradaySpecError(
            "signals.evaluation.evaluate_on_minutes "
            f"must equal {expected_minutes}."
        )

    entry_modes = _require_mapping(
        section.get("entry_modes"),
        "signals.entry_modes",
    )

    for mode_name in [
        "confirmed",
        "divergence",
    ]:
        mode = _require_mapping(
            entry_modes.get(mode_name),
            (
                "signals.entry_modes."
                f"{mode_name}"
            ),
        )

        _require_bool(
            mode.get("enabled"),
            (
                "signals.entry_modes."
                f"{mode_name}.enabled"
            ),
        )

        for key, value in mode.items():
            if key.startswith(
                "minimum_absolute_"
            ):
                _require_number(
                    value,
                    (
                        "signals.entry_modes."
                        f"{mode_name}.{key}"
                    ),
                    minimum=0.0,
                )

    _require_int(
        section.get(
            "signal_expiry_minutes"
        ),
        "signals.signal_expiry_minutes",
        minimum=1,
    )

    _require_bool(
        section.get(
            "one_open_position_per_relationship"
        ),
        (
            "signals."
            "one_open_position_per_relationship"
        ),
    )

    pyramiding = _require_bool(
        section.get(
            "pyramiding_enabled"
        ),
        "signals.pyramiding_enabled",
    )

    if pyramiding:
        raise IntradaySpecError(
            "Pyramiding is not supported in the "
            "first paper-trading candidate."
        )


def _validate_execution(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["execution"],
        "execution",
    )

    order_type = _require_string(
        section.get("order_type"),
        "execution.order_type",
    )

    if order_type != "simulated_market":
        raise IntradaySpecError(
            "execution.order_type must be "
            "simulated_market."
        )

    entry_timing = _require_string(
        section.get("entry_timing"),
        "execution.entry_timing",
    )

    if (
        entry_timing
        != "next_completed_one_minute_bar_open"
    ):
        raise IntradaySpecError(
            "Unsupported execution.entry_timing."
        )

    _require_int(
        section.get(
            "additional_entry_delay_minutes"
        ),
        (
            "execution."
            "additional_entry_delay_minutes"
        ),
        minimum=0,
    )

    fill_model = _require_mapping(
        section.get("fill_model"),
        "execution.fill_model",
    )

    fallback_cost = _require_number(
        fill_model.get(
            "fallback_minimum_round_trip_cost_bps"
        ),
        (
            "execution.fill_model."
            "fallback_minimum_round_trip_cost_bps"
        ),
        minimum=0.0,
    )

    _require_number(
        fill_model.get(
            "fallback_slippage_per_side_bps"
        ),
        (
            "execution.fill_model."
            "fallback_slippage_per_side_bps"
        ),
        minimum=0.0,
    )

    rejection_limit = _require_number(
        section.get(
            "reject_entry_when_expected_"
            "round_trip_cost_bps_exceeds"
        ),
        (
            "execution."
            "reject_entry_when_expected_"
            "round_trip_cost_bps_exceeds"
        ),
        minimum=0.0,
    )

    if rejection_limit < fallback_cost:
        raise IntradaySpecError(
            "The entry cost rejection limit cannot "
            "be below the fallback transaction cost."
        )


def _validate_position_sizing(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["position_sizing"],
        "position_sizing",
    )

    method = _require_string(
        section.get("method"),
        "position_sizing.method",
    )

    if method != "signal_volatility":
        raise IntradaySpecError(
            "position_sizing.method must remain "
            "signal_volatility."
        )

    floor = _require_number(
        section.get(
            "signal_strength_floor"
        ),
        (
            "position_sizing."
            "signal_strength_floor"
        ),
        minimum=0.0,
    )

    cap = _require_number(
        section.get(
            "signal_strength_cap"
        ),
        (
            "position_sizing."
            "signal_strength_cap"
        ),
        minimum=0.0,
    )

    if cap < floor:
        raise IntradaySpecError(
            "Signal strength cap must be at least "
            "the signal strength floor."
        )


def _validate_risk(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["risk"],
        "risk",
    )

    _require_int(
        section.get(
            "maximum_simultaneous_positions"
        ),
        (
            "risk."
            "maximum_simultaneous_positions"
        ),
        minimum=1,
    )

    relationship_exposure = (
        _require_number(
            section.get(
                "maximum_relationship_exposure_pct"
            ),
            (
                "risk."
                "maximum_relationship_exposure_pct"
            ),
            minimum=0.0,
            maximum=1.0,
        )
    )

    currency_exposure = _require_number(
        section.get(
            "maximum_currency_exposure_pct"
        ),
        (
            "risk."
            "maximum_currency_exposure_pct"
        ),
        minimum=0.0,
        maximum=1.0,
    )

    gross_exposure = _require_number(
        section.get(
            "maximum_gross_exposure_pct"
        ),
        (
            "risk."
            "maximum_gross_exposure_pct"
        ),
        minimum=0.0,
        maximum=1.0,
    )

    if relationship_exposure > currency_exposure:
        raise IntradaySpecError(
            "Relationship exposure cannot exceed "
            "currency exposure."
        )

    if currency_exposure > gross_exposure:
        raise IntradaySpecError(
            "Currency exposure cannot exceed "
            "gross exposure."
        )

    _require_number(
        section.get(
            "daily_loss_pause_pct"
        ),
        "risk.daily_loss_pause_pct",
        minimum=0.0,
        maximum=100.0,
    )

    _require_number(
        section.get(
            "total_drawdown_pause_pct"
        ),
        "risk.total_drawdown_pause_pct",
        minimum=0.0,
        maximum=100.0,
    )


def _validate_exits(
    spec: dict[str, Any],
) -> None:
    section = _require_mapping(
        spec["exits"],
        "exits",
    )

    maximum_holding = _require_mapping(
        section.get(
            "maximum_holding_time"
        ),
        "exits.maximum_holding_time",
    )

    _require_bool(
        maximum_holding.get("enabled"),
        (
            "exits.maximum_holding_time."
            "enabled"
        ),
    )

    _require_int(
        maximum_holding.get("minutes"),
        (
            "exits.maximum_holding_time."
            "minutes"
        ),
        minimum=1,
    )

    convergence = _require_mapping(
        section.get(
            "divergence_convergence"
        ),
        "exits.divergence_convergence",
    )

    _require_number(
        convergence.get(
            "close_when_absolute_"
            "divergence_below"
        ),
        (
            "exits.divergence_convergence."
            "close_when_absolute_"
            "divergence_below"
        ),
        minimum=0.0,
    )

    volatility_stop = _require_mapping(
        section.get(
            "volatility_stop"
        ),
        "exits.volatility_stop",
    )

    _require_number(
        volatility_stop.get(
            "volatility_units"
        ),
        (
            "exits.volatility_stop."
            "volatility_units"
        ),
        minimum=0.0,
    )


def _validate_operational_sections(
    spec: dict[str, Any],
) -> None:
    sessions = _require_mapping(
        spec["sessions"],
        "sessions",
    )

    _require_int(
        sessions.get(
            "block_new_entries_before_"
            "market_close_minutes"
        ),
        (
            "sessions."
            "block_new_entries_before_"
            "market_close_minutes"
        ),
        minimum=0,
    )

    _require_bool(
        sessions.get(
            "allow_overnight_positions"
        ),
        (
            "sessions."
            "allow_overnight_positions"
        ),
    )

    paper_ledger = _require_mapping(
        spec["paper_ledger"],
        "paper_ledger",
    )

    for key in [
        "persist_every_decision",
        "persist_every_order",
        "persist_every_fill",
        "mark_positions_to_market_every_minute",
    ]:
        _require_bool(
            paper_ledger.get(key),
            f"paper_ledger.{key}",
        )

    monitoring = _require_mapping(
        spec["monitoring"],
        "monitoring",
    )

    _require_int(
        monitoring.get(
            "heartbeat_interval_seconds"
        ),
        (
            "monitoring."
            "heartbeat_interval_seconds"
        ),
        minimum=1,
    )

    dashboard = _require_mapping(
        spec["dashboard"],
        "dashboard",
    )

    _require_int(
        dashboard.get(
            "refresh_interval_seconds"
        ),
        (
            "dashboard."
            "refresh_interval_seconds"
        ),
        minimum=1,
    )

    sections = _require_list(
        dashboard.get("sections"),
        "dashboard.sections",
    )

    if not sections:
        raise IntradaySpecError(
            "dashboard.sections cannot be empty."
        )

    governance = _require_mapping(
        spec["governance"],
        "governance",
    )

    formal_review = _require_mapping(
        governance.get(
            "formal_strategy_review"
        ),
        (
            "governance."
            "formal_strategy_review"
        ),
    )

    _require_int(
        formal_review.get(
            "minimum_calendar_weeks"
        ),
        (
            "governance."
            "formal_strategy_review."
            "minimum_calendar_weeks"
        ),
        minimum=1,
    )

    _require_int(
        formal_review.get(
            "minimum_closed_trades"
        ),
        (
            "governance."
            "formal_strategy_review."
            "minimum_closed_trades"
        ),
        minimum=1,
    )


def validate_intraday_spec(
    spec: dict[str, Any],
) -> dict[str, Any]:
    _require_keys(
        spec,
        REQUIRED_TOP_LEVEL_SECTIONS,
        "root",
    )

    _validate_strategy(spec)
    _validate_capital(spec)
    _validate_market_data(spec)
    _validate_news(spec)

    relationship_metadata_path = (
        _validate_relationships(spec)
    )

    _validate_features(spec)
    _validate_signals(spec)
    _validate_execution(spec)
    _validate_position_sizing(spec)
    _validate_risk(spec)
    _validate_exits(spec)
    _validate_operational_sections(spec)

    validated = deepcopy(spec)

    validated["_runtime"] = {
        "project_root": str(
            PROJECT_ROOT
        ),
        "relationship_metadata_path": str(
            relationship_metadata_path
        ),
    }

    return validated


def load_intraday_spec(
    path: str | Path = DEFAULT_SPEC_PATH,
) -> dict[str, Any]:
    spec_path = Path(path)

    if not spec_path.is_absolute():
        spec_path = (
            PROJECT_ROOT
            / spec_path
        )

    spec_path = spec_path.resolve()

    if not spec_path.exists():
        raise FileNotFoundError(
            "Intraday strategy specification "
            f"does not exist: {spec_path}"
        )

    try:
        raw = yaml.safe_load(
            spec_path.read_text(
                encoding="utf-8"
            )
        )
    except yaml.YAMLError as exc:
        raise IntradaySpecError(
            "The intraday specification contains "
            f"invalid YAML: {exc}"
        ) from exc

    spec = _require_mapping(
        raw,
        "root",
    )

    validated = validate_intraday_spec(
        spec
    )

    validated["_runtime"][
        "specification_path"
    ] = str(spec_path)

    return validated


def main() -> None:
    spec = load_intraday_spec()

    strategy = spec["strategy"]
    data = spec["data"]
    relationships = spec[
        "relationships"
    ]
    risk = spec["risk"]
    execution = spec["execution"]

    print(
        "Intraday strategy specification "
        "validated successfully."
    )

    print(
        f"Name: "
        f"{strategy['name']}"
    )

    print(
        f"Specification version: "
        f"{strategy['specification_version']}"
    )

    print(
        f"Status: "
        f"{strategy['status']}"
    )

    print(
        f"Market interval: "
        f"{data['market']['input_bar_interval']}"
    )

    print(
        f"Signal interval: "
        f"{data['market']['signal_evaluation_interval']}"
    )

    print(
        "News sources: "
        + ", ".join(
            data["news"]["sources"]
        )
    )

    print(
        f"Relationship metadata: "
        f"{spec['_runtime']['relationship_metadata_path']}"
    )

    print(
        f"Weak relationship weight: "
        f"{relationships['weak_relationship_weight']}"
    )

    print(
        f"Maximum positions: "
        f"{risk['maximum_simultaneous_positions']}"
    )

    print(
        f"Maximum gross exposure: "
        f"{risk['maximum_gross_exposure_pct']:.2%}"
    )

    print(
        "Maximum expected round-trip cost: "
        f"{execution[
            'reject_entry_when_expected_'
            'round_trip_cost_bps_exceeds'
        ]:.2f} bps"
    )

    print(
        f"Paper trading only: "
        f"{strategy['paper_trading_only']}"
    )

    print(
        f"Live capital approved: "
        f"{strategy['live_capital_approved']}"
    )


if __name__ == "__main__":
    main()
