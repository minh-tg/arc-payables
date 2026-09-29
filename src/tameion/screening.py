"""Screening providers.

OpenSanctions is the first real provider, behind a provider interface so a commercial
screening service (or an on-premise deployment) can be added without touching the policy.

Contract used (from the official API documentation):

* ``POST {base}/match/{dataset}`` with body ``{"queries": {"<id>": {"schema": ..., "properties": {...}}}}``
* header ``Authorization: ApiKey <OPENSANCTIONS_API_KEY>``
* response ``{"responses": {"<id>": {"status": 200, "results": [ ... ], "total": {...}}}, "limit": N}``
* each result carries ``id``, ``caption``, ``schema``, ``properties``, ``datasets``,
  ``target``, ``score`` and ``match``. ``score`` is match confidence, **not** a risk score,
  and ``match`` is true at or above the algorithm threshold (0.7 by default).

Classification is deterministic and recorded as evidence. The provider must fail closed:
anything that prevents a completed screening produces ``UNAVAILABLE``, which the policy
treats as a human-review blocker rather than as a clear result.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx

from .domain import ScreeningStatus, SupplierRecord, utcnow
from .store import canonical_json

# OpenSanctions schema names used for the two queries (FollowTheMoney schemata).
ENTITY_SCHEMA = "Company"
WALLET_SCHEMA = "CryptoWallet"
ENTITY_QUERY_KEY = "supplier_entity"
WALLET_QUERY_KEY = "supplier_wallet"

# Risk topics that make a matched entity a target rather than a merely related party.
RISK_TOPIC_PREFIXES = ("sanction", "debarment", "corp.disqual", "crime", "terror")


@dataclass(frozen=True)
class ScreeningMatch:
    query: str
    entity_id: str
    caption: str
    schema: str | None
    score: float
    matched: bool
    target: bool
    datasets: tuple[str, ...]
    topics: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "entity_id": self.entity_id,
            "caption": self.caption,
            "schema": self.schema,
            "score": self.score,
            "matched": self.matched,
            "target": self.target,
            "datasets": list(self.datasets),
            "topics": list(self.topics),
        }


@dataclass(frozen=True)
class ScreeningResult:
    status: ScreeningStatus
    provider: str
    subject: str
    checked_at: datetime = field(default_factory=utcnow)
    dataset: str | None = None
    wallet: str | None = None
    matches: tuple[ScreeningMatch, ...] = ()
    reason: str = ""
    response_hash: str | None = None

    @property
    def clear(self) -> bool:
        return self.status == ScreeningStatus.CLEAR

    @property
    def strongest(self) -> ScreeningMatch | None:
        if not self.matches:
            return None
        return max(self.matches, key=lambda item: (item.matched, item.target, item.score))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "provider": self.provider,
            "subject": self.subject,
            "dataset": self.dataset,
            "wallet": self.wallet,
            "checked_at": self.checked_at.isoformat(),
            "reason": self.reason,
            "response_hash": self.response_hash,
            "matches": [match.to_dict() for match in self.matches],
        }


class ScreeningProvider(Protocol):
    name: str

    def screen(self, supplier: SupplierRecord | None, wallet: str | None) -> ScreeningResult: ...


class UnavailableScreeningProvider:
    """Always fails closed. Used for failure injection and when no provider is configured."""

    name = "unavailable"

    def screen(self, supplier: SupplierRecord | None, wallet: str | None) -> ScreeningResult:
        return ScreeningResult(
            status=ScreeningStatus.UNAVAILABLE,
            provider=self.name,
            subject=supplier.name if supplier else "unknown",
            wallet=wallet,
            reason="No screening provider is configured; screening could not be completed.",
        )


class FixtureScreeningProvider:
    """Deterministic provider backed by seeded fixtures. Used by the local demo and tests."""

    name = "fixture"

    def __init__(self, store, default_status: ScreeningStatus = ScreeningStatus.UNAVAILABLE):
        self.store = store
        self.default_status = default_status

    def screen(self, supplier: SupplierRecord | None, wallet: str | None) -> ScreeningResult:
        status = self.default_status
        if wallet:
            fixture = self.store.get_fixture("screening", wallet.lower())
            if fixture:
                status = ScreeningStatus(fixture["status"])
        if supplier and not wallet:
            raw = self.store.get_supplier_fixture(supplier.id)
            if raw and raw.get("screening"):
                status = ScreeningStatus(raw["screening"])
        return ScreeningResult(
            status=status,
            provider=self.name,
            subject=supplier.name if supplier else "unknown",
            wallet=wallet,
            reason=f"Fixture screening result: {status.value}.",
        )


class OpenSanctionsScreener:
    """OpenSanctions matching API client with deterministic, fail-closed classification."""

    name = "opensanctions"

    def __init__(
        self,
        settings,
        client: httpx.Client | None = None,
    ):
        self.settings = settings
        self.base_url = (getattr(settings, "opensanctions_base_url", "") or "https://api.opensanctions.org").rstrip("/")
        self.dataset = getattr(settings, "opensanctions_dataset", "default") or "default"
        self.api_key = getattr(settings, "opensanctions_api_key", None)
        self.review_threshold = float(getattr(settings, "opensanctions_review_threshold", 0.5))
        self.limit = int(getattr(settings, "opensanctions_limit", 5))
        self.positive_match_policy = (getattr(settings, "screening_positive_match_policy", "block") or "block").lower()
        self.client = client or httpx.Client(timeout=float(getattr(settings, "screening_timeout_seconds", 15.0)))

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _queries(self, supplier: SupplierRecord | None, wallet: str | None) -> dict[str, dict]:
        queries: dict[str, dict] = {}
        if supplier and supplier.name:
            properties: dict[str, list[str]] = {"name": [supplier.name]}
            if supplier.erp_supplier_id:
                properties["idNumber"] = [supplier.erp_supplier_id]
            queries[ENTITY_QUERY_KEY] = {"schema": ENTITY_SCHEMA, "properties": properties}
        if wallet:
            queries[WALLET_QUERY_KEY] = {
                "schema": WALLET_SCHEMA,
                "properties": {"publicKey": [wallet], "currencySymbol": ["USDC"]},
            }
        return queries

    def screen(self, supplier: SupplierRecord | None, wallet: str | None) -> ScreeningResult:
        subject = supplier.name if supplier else "unknown"
        if not self.configured:
            return ScreeningResult(
                status=ScreeningStatus.UNAVAILABLE,
                provider=self.name,
                subject=subject,
                wallet=wallet,
                dataset=self.dataset,
                reason="OPENSANCTIONS_API_KEY is not configured; screening could not be completed.",
            )
        queries = self._queries(supplier, wallet)
        if not queries:
            return ScreeningResult(
                status=ScreeningStatus.UNAVAILABLE,
                provider=self.name,
                subject=subject,
                wallet=wallet,
                dataset=self.dataset,
                reason="No screenable supplier identity or wallet was available.",
            )
        try:
            response = self.client.post(
                f"{self.base_url}/match/{self.dataset}",
                headers={"Authorization": f"ApiKey {self.api_key}", "Accept": "application/json"},
                json={"queries": queries, "limit": self.limit},
            )
            if response.status_code >= 400:
                return self._unavailable(subject, wallet, f"OpenSanctions returned HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("responses"), dict):
                return self._unavailable(subject, wallet, "OpenSanctions returned an unrecognised response body")
        except httpx.HTTPError as exc:
            return self._unavailable(subject, wallet, f"OpenSanctions request failed ({type(exc).__name__})")
        except ValueError:
            return self._unavailable(subject, wallet, "OpenSanctions returned invalid JSON")

        matches: list[ScreeningMatch] = []
        for query_key, result in payload["responses"].items():
            if not isinstance(result, dict):
                continue
            status_code = result.get("status")
            if isinstance(status_code, int) and status_code >= 400:
                # A rejected sub-query means screening did not complete for that subject.
                return self._unavailable(subject, wallet, f"OpenSanctions rejected the {query_key} query")
            for row in result.get("results") or []:
                if not isinstance(row, dict):
                    continue
                properties = row.get("properties") or {}
                matches.append(
                    ScreeningMatch(
                        query=str(query_key),
                        entity_id=str(row.get("id") or ""),
                        caption=str(row.get("caption") or ""),
                        schema=str(row.get("schema")) if row.get("schema") else None,
                        score=float(row.get("score") or 0.0),
                        matched=bool(row.get("match")),
                        target=bool(row.get("target")),
                        datasets=tuple(sorted(str(item) for item in (row.get("datasets") or ()))),
                        topics=tuple(sorted(str(item) for item in (properties.get("topics") or ()))),
                    )
                )

        status, reason = self._classify(matches)
        return ScreeningResult(
            status=status,
            provider=self.name,
            subject=subject,
            wallet=wallet,
            dataset=self.dataset,
            matches=tuple(matches),
            reason=reason,
            response_hash="0x" + hashlib.sha256(canonical_json(payload).encode()).hexdigest(),
        )

    def _classify(self, matches: list[ScreeningMatch]) -> tuple[ScreeningStatus, str]:
        risk_hits = [m for m in matches if m.matched and self._is_risk(m)]
        other_matches = [m for m in matches if m.matched and not self._is_risk(m)]
        near_misses = [m for m in matches if not m.matched and m.score >= self.review_threshold]

        if risk_hits:
            top = max(risk_hits, key=lambda item: item.score)
            return (
                ScreeningStatus.FLAGGED,
                f"Sanctions/risk screening matched {top.caption or top.entity_id} "
                f"(score {top.score:.2f}, topics {', '.join(top.topics) or 'n/a'}).",
            )
        if other_matches:
            top = max(other_matches, key=lambda item: item.score)
            return (
                ScreeningStatus.INCONCLUSIVE,
                f"Ambiguous screening match for {top.caption or top.entity_id} "
                f"(score {top.score:.2f}); human review required.",
            )
        if near_misses:
            top = max(near_misses, key=lambda item: item.score)
            return (
                ScreeningStatus.INCONCLUSIVE,
                f"No accepted match, but {top.caption or top.entity_id} scored {top.score:.2f} "
                f"at or above the review threshold {self.review_threshold:.2f}; human review required.",
            )
        return (
            ScreeningStatus.CLEAR,
            "Screening completed with no match at or above the review threshold.",
        )

    @staticmethod
    def _is_risk(match: ScreeningMatch) -> bool:
        if match.target:
            return True
        return any(topic.startswith(RISK_TOPIC_PREFIXES) for topic in match.topics)

    def _unavailable(self, subject: str, wallet: str | None, reason: str) -> ScreeningResult:
        return ScreeningResult(
            status=ScreeningStatus.UNAVAILABLE,
            provider=self.name,
            subject=subject,
            wallet=wallet,
            dataset=self.dataset,
            reason=f"{reason}; payment remains blocked.",
        )


def build_screening_provider(settings, store) -> ScreeningProvider:
    choice = (getattr(settings, "screening_provider", "fixture") or "fixture").lower()
    if choice == "opensanctions":
        return OpenSanctionsScreener(settings)
    if choice == "unavailable":
        return UnavailableScreeningProvider()
    return FixtureScreeningProvider(store)
