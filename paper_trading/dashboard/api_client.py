from __future__ import annotations

import os
from typing import Any, Literal

import requests


DEFAULT_API_BASE_URL = os.getenv(
    "PAPER_TRADING_API_BASE_URL",
    "http://127.0.0.1:8000",
)
DEFAULT_TIMEOUT_SECONDS = 5.0


class ApiClientError(RuntimeError):
    """Raised when the read-only API cannot be reached or returns an error."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ApiNotFoundError(ApiClientError):
    """Raised when the API returns HTTP 404 (e.g. unknown run_id)."""


class ApiUnavailableError(ApiClientError):
    """Raised when the API reports its database is unreachable (HTTP 503)."""


class ApiClient:
    """Thin, read-only client for the paper-trading FastAPI backend.

    The dashboard must consume this client rather than querying SQLite
    directly, per the intraday spec's API boundary rule.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_API_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        clean_params = (
            {k: v for k, v in params.items() if v is not None}
            if params
            else None
        )
        try:
            response = self._session.get(
                url,
                params=clean_params,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise ApiClientError(
                f"Cannot reach API at {self.base_url}: {exc}"
            ) from exc

        if response.status_code == 404:
            detail = _safe_detail(response)
            raise ApiNotFoundError(detail, status_code=404)
        if response.status_code == 503:
            detail = _safe_detail(response)
            raise ApiUnavailableError(detail, status_code=503)
        if response.status_code != 200:
            detail = _safe_detail(response)
            raise ApiClientError(detail, status_code=response.status_code)

        return response.json()

    # -- system ------------------------------------------------------

    def root(self) -> dict[str, Any]:
        return self._get("/")

    def health(self) -> dict[str, Any]:
        return self._get("/health")

    def services(self) -> dict[str, Any]:
        return self._get("/services")

    def alerts(
        self,
        *,
        run_id: str | None = None,
        resolved: bool | None = False,
        severity: Literal["info", "warning", "critical"] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._get(
            "/alerts",
            {
                "run_id": run_id,
                "resolved": resolved,
                "severity": severity,
                "limit": limit,
            },
        )

    # -- strategy / runs ----------------------------------------------

    def strategy(self, *, run_id: str | None = None) -> dict[str, Any]:
        return self._get("/strategy", {"run_id": run_id})

    def current_run(self, *, run_id: str | None = None) -> dict[str, Any]:
        return self._get("/runs/current", {"run_id": run_id})

    def relationships(self, *, active_only: bool = True) -> dict[str, Any]:
        return self._get("/relationships", {"active_only": active_only})

    # -- signals -------------------------------------------------------

    def features_latest(
        self,
        *,
        run_id: str | None = None,
        complete_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._get(
            "/features/latest",
            {
                "run_id": run_id,
                "complete_only": complete_only,
                "limit": limit,
            },
        )

    def signals_latest(
        self,
        *,
        run_id: str | None = None,
        approved_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._get(
            "/signals/latest",
            {
                "run_id": run_id,
                "approved_only": approved_only,
                "limit": limit,
            },
        )

    # -- portfolio -------------------------------------------------------

    def positions(
        self,
        *,
        run_id: str | None = None,
        status: Literal["open", "closed", "all"] = "open",
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._get(
            "/positions",
            {"run_id": run_id, "status": status, "limit": limit},
        )

    def orders(
        self,
        *,
        run_id: str | None = None,
        status: str = "all",
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._get(
            "/orders",
            {"run_id": run_id, "status": status, "limit": limit},
        )

    def fills(
        self,
        *,
        run_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        return self._get("/fills", {"run_id": run_id, "limit": limit})

    def equity(
        self,
        *,
        run_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        return self._get("/equity", {"run_id": run_id, "limit": limit})

    # -- news -------------------------------------------------------

    def news_latest(
        self,
        *,
        asset: str | None = None,
        asset_type: Literal["commodity", "currency"] | None = None,
        min_confidence: float = 0.0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self._get(
            "/news/latest",
            {
                "asset": asset,
                "asset_type": asset_type,
                "min_confidence": min_confidence,
                "limit": limit,
            },
        )

    # -- dashboard -------------------------------------------------------

    def summary(self, *, run_id: str | None = None) -> dict[str, Any]:
        return self._get("/summary", {"run_id": run_id})


def _safe_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(payload, dict) and "detail" in payload:
        return str(payload["detail"])
    return str(payload)
