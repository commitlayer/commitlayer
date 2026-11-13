"""CommitLayer for x402: observe the payment protocol on the agent's HTTP client and record what was bought.

x402 (version 2) is three headers on an HTTP exchange: a server answers ``402 Payment Required`` with a quote in
``PAYMENT-REQUIRED`` (base64 JSON: ``x402Version``, ``resource``, ``accepts[]`` with ``scheme``, ``network``,
``amount``, ``asset``, ``payTo``, ``maxTimeoutSeconds``), the client pays by retrying with ``PAYMENT-SIGNATURE``
(base64 JSON: ``accepted``, ``payload.authorization.nonce`` for the exact scheme), and the final response carries
the settlement in ``PAYMENT-RESPONSE`` (base64 JSON: ``success``, ``transaction``, ``network``, ``payer``,
``errorReason``).

This package does not pay, sign or hold a key. It watches those three headers on an ``httpx`` client (event hooks)
or on any client through :meth:`X402Observer.on_request` / :meth:`X402Observer.on_response`, and:

- records the purchase as a CommitLayer action (``purchase_<kind>``) on the subject ``<nonce>`` when the client
  sends ``PAYMENT-SIGNATURE``, with the accepted quote's amount, asset, network, ``payTo``, resource and provider;
- pushes a ``settlement`` fact (``status`` settled, failed or pending) from ``PAYMENT-RESPONSE`` and a ``delivered``
  or ``delivery_failed`` fact from the HTTP outcome of the paid request.

The facts go through the ``evidence`` client when one is given (a gateway or backend holding a ``facts:write``
key, classed ``customer_backend`` by the service), else through the recorder client, in which case the service
classes them ``agent_self_report``: the agent's own observation, shown but never counted as proof.

Nothing sensitive is kept: no body, no signature value, no ``Authorization`` header, no wallet or facilitator
credential. The only state is the last quote per URL. Recording is fail-open: a CommitLayer error is logged and the
purchase proceeds.

The agent may declare its intent. It may read the verdict. It must never control the independent facts, the
governing contract, or the final evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "KINDS",
    "Purchase",
    "Quote",
    "X402Observer",
    "amount_from_atomic",
    "decode_header",
    "settlement_status",
    "__version__",
]
__version__ = "0.1.3"  # kept equal to pyproject.toml; tests check it

log = logging.getLogger("commitlayer.x402")

KINDS = ("api_call", "browser_session", "dataset", "model_inference", "compute_resource", "mcp_tool_call")

# v2 header names, and the v1 spellings recognised on a best-effort basis (the v1 text was not re-read).
H_REQUIRED = ("payment-required", "x-payment-required")
H_SIGNATURE = ("payment-signature", "x-payment")
H_RESPONSE = ("payment-response", "x-payment-response")


def decode_header(value: str | None) -> dict[str, Any] | None:
    """A base64 JSON header value as a dict; ``None`` when absent or not decodable. Never raises."""
    if not value:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=False)
        out = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return out if isinstance(out, dict) else None


def amount_from_atomic(atomic: Any, decimals: int | None) -> float | None:
    """``accepts[].amount`` (a string of atomic units) in the asset's major units; ``None`` when unknown."""
    try:
        v = Decimal(str(atomic))
    except (InvalidOperation, ValueError):
        return None
    if decimals is None:
        return float(v)
    return float(v / (Decimal(10) ** int(decimals)))


def settlement_status(response_header: dict[str, Any]) -> str:
    """``settled``, ``pending`` (the facilitator's non-terminal ``settlement_pending``) or ``failed``."""
    if response_header.get("success") is True:
        return "settled"
    reason = str(response_header.get("errorReason") or response_header.get("error_reason") or "")
    return "pending" if reason == "settlement_pending" else "failed"


def _header(headers: Any, names: tuple[str, ...]) -> str | None:
    if headers is None:
        return None
    for n in names:
        try:
            v = headers.get(n)
        except Exception:  # noqa: BLE001 - a header map of any shape
            v = None
        if v:
            return str(v)
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Quote:
    """One ``PaymentRequirements`` entry of a 402 (``accepts[i]``) together with the resource it is for."""

    url: str
    scheme: str | None
    network: str | None
    amount_atomic: str | None
    asset: str | None
    pay_to: str | None
    max_timeout_seconds: int | None
    resource: dict[str, Any] = field(default_factory=dict)


@dataclass
class Purchase:
    """What the observer recorded for one paid request."""

    action_id: str
    subject_id: str
    url: str
    quote: Quote | None
    recorded: dict[str, Any] | None = None
    settlement: str | None = None
    delivered: bool | None = None


class X402Observer:
    """Watch x402 headers and record purchases. ``recorder`` is a ``commitlayer.Client`` with a recorder key
    (``actions:record``); ``evidence`` an optional client with a ``facts:write`` key for the gateway role.
    ``kind`` is one of :data:`KINDS` and selects the pack contract (``x402_purchase_<kind>``); ``decimals`` is the
    asset's (USDC: 6; ``None`` keeps atomic units)."""

    def __init__(
        self,
        recorder: Any,
        *,
        kind: str = "api_call",
        evidence: Any | None = None,
        contract_id: str | None = None,
        contract_version: int | None = None,
        decimals: int | None = 6,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        extra_params: dict[str, Any] | None = None,
        fail_open: bool = True,
    ):
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        self.recorder = recorder
        self.evidence = evidence
        self.kind = kind
        self.contract_id = contract_id or f"x402_purchase_{kind}"
        self.contract_version = contract_version
        self.decimals = decimals
        self.agent_id = agent_id
        self.workflow_id = workflow_id
        self.extra_params = dict(extra_params or {})
        self.fail_open = fail_open
        self.quotes: dict[str, Quote] = {}
        self.purchases: list[Purchase] = []
        self._pending: dict[int, Purchase] = {}

    # ---------------- protocol events ----------------

    def on_response(self, url: str, status: int, headers: Any, *, request_headers: Any = None, request_id: Any = None, content_length: int | None = None) -> None:
        """A response of the client. A 402 with ``PAYMENT-REQUIRED`` is a quote; a response to a request that
        carried ``PAYMENT-SIGNATURE`` closes a purchase (settlement from ``PAYMENT-RESPONSE``, delivery from the status)."""
        if status == 402:
            quoted = decode_header(_header(headers, H_REQUIRED))
            if quoted:
                self._remember_quote(url, quoted)
        p = self._pending.pop(id(request_id), None) if request_id is not None else None
        if p is None and request_headers is not None and _header(request_headers, H_SIGNATURE):
            p = self.on_request(url, request_headers)
        if p is None:
            return
        settlement = decode_header(_header(headers, H_RESPONSE))
        facts: list[dict[str, Any]] = []
        now = _now()
        if settlement is not None:
            st = settlement_status(settlement)
            p.settlement = st
            attrs: dict[str, Any] = {
                "status": st,
                "transaction": settlement.get("transaction"),
                "network": settlement.get("network") or (p.quote.network if p.quote else None),
                "payer": settlement.get("payer"),
            }
            if p.quote is not None:
                attrs.update(self._amounts(p.quote))
                attrs["asset"] = p.quote.asset
                attrs["pay_to"] = p.quote.pay_to
            if st != "settled":
                attrs["error_reason"] = settlement.get("errorReason") or settlement.get("error_reason")
            facts.append(
                {
                    "source": "x402",
                    "subject_id": p.subject_id,
                    "subject_kind": "payment",
                    "fact_type": "settlement",
                    "occurred_at": now,
                    "attributes": {k: v for k, v in attrs.items() if v is not None},
                    "raw_ref": f"x402:settlement:{p.subject_id}:{st}",
                }
            )
        delivered = 200 <= status < 300
        p.delivered = delivered
        dattrs: dict[str, Any] = {"kind": self.kind, "http_status": status, "resource": url}
        if content_length is not None:
            dattrs["bytes"] = content_length
        facts.append(
            {
                "source": "delivery",
                "subject_id": p.subject_id,
                "subject_kind": "payment",
                "fact_type": "delivered" if delivered else "delivery_failed",
                "occurred_at": now,
                "attributes": dattrs,
                "raw_ref": f"x402:delivery:{p.subject_id}",
            }
        )
        self._push(facts)

    def on_request(self, url: str, headers: Any, *, request_id: Any = None) -> Purchase | None:
        """A request of the client. One carrying ``PAYMENT-SIGNATURE`` is a purchase: record the action now, from
        the accepted quote and the payment's nonce. Returns the purchase, or ``None`` when the request is not paid."""
        sig = decode_header(_header(headers, H_SIGNATURE))
        if sig is None:
            return None
        accepted = sig.get("accepted") if isinstance(sig.get("accepted"), dict) else None
        quote = self._quote_from(url, accepted) if accepted else self.quotes.get(url)
        nonce = self._nonce(sig)
        subject = nonce or ("sig:" + hashlib.sha256(str(_header(headers, H_SIGNATURE)).encode()).hexdigest()[:40])
        params: dict[str, Any] = {"resource": url, "provider": urlsplit(url).hostname or "", **self.extra_params}
        if quote is not None:
            params.update(self._amounts(quote))
            params.update({"asset": quote.asset, "network": quote.network, "pay_to": quote.pay_to, "scheme": quote.scheme})
        payer = self._payer(sig)
        if payer:
            params["payer"] = payer
        params = {k: v for k, v in params.items() if v is not None}
        p = Purchase(action_id=f"purchase:{subject[:66]}", subject_id=subject, url=url, quote=quote)
        try:
            p.recorded = self.recorder.record_action(
                self.contract_id,
                subject,
                action_id=p.action_id,
                action_type=f"purchase_{self.kind}",
                subject_kind="payment",
                params=params,
                workflow_id=self.workflow_id,
                agent_id=self.agent_id,
                channel="x402",
                contract_version=self.contract_version,
            )
        except Exception as e:  # noqa: BLE001 - the purchase must not depend on the verifier
            if not self.fail_open:
                raise
            log.warning("commitlayer x402: purchase %s not recorded: %s", p.action_id, e)
        self.purchases.append(p)
        if request_id is not None:
            self._pending[id(request_id)] = p
        return p

    # ---------------- httpx ----------------

    def httpx_hooks(self) -> dict[str, list[Any]]:
        """``event_hooks`` for ``httpx.Client``: ``httpx.Client(event_hooks=observer.httpx_hooks())``. The hooks
        are synchronous functions; ``httpx.AsyncClient`` awaits its hooks and cannot take them. With an async
        client, call :meth:`on_request` and :meth:`on_response` yourself, from a worker thread."""

        def on_request(request: Any) -> None:
            if _header(request.headers, H_SIGNATURE):
                self.on_request(str(request.url), request.headers, request_id=request)

        def on_response(response: Any) -> None:
            req = response.request
            length = response.headers.get("content-length")
            self.on_response(
                str(req.url),
                response.status_code,
                response.headers,
                request_headers=req.headers,
                request_id=req,
                content_length=int(length) if length and str(length).isdigit() else None,
            )

        return {"request": [on_request], "response": [on_response]}

    @property
    def last(self) -> Purchase | None:
        return self.purchases[-1] if self.purchases else None

    # ---------------- helpers ----------------

    def _remember_quote(self, url: str, required: dict[str, Any]) -> None:
        accepts = required.get("accepts")
        first = accepts[0] if isinstance(accepts, list) and accepts and isinstance(accepts[0], dict) else None
        if first is None:
            return
        self.quotes[url] = self._quote_from(url, first, required.get("resource"))

    def _quote_from(self, url: str, req: dict[str, Any], resource: Any = None) -> Quote:
        amount = req.get("amount")
        if amount is None:
            amount = req.get("maxAmountRequired")  # v1 spelling, best effort
        timeout = req.get("maxTimeoutSeconds")
        return Quote(
            url=url,
            scheme=req.get("scheme"),
            network=req.get("network"),
            amount_atomic=str(amount) if amount is not None else None,
            asset=req.get("asset"),
            pay_to=req.get("payTo"),
            max_timeout_seconds=int(timeout) if isinstance(timeout, int | float) else None,
            resource=resource if isinstance(resource, dict) else {},
        )

    def _amounts(self, quote: Quote) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if quote.amount_atomic is not None:
            out["amount_atomic"] = quote.amount_atomic
            major = amount_from_atomic(quote.amount_atomic, self.decimals)
            if major is not None:
                out["amount"] = major
        return out

    @staticmethod
    def _nonce(sig: dict[str, Any]) -> str | None:
        payload = sig.get("payload")
        if not isinstance(payload, dict):
            return None
        auth = payload.get("authorization")
        if isinstance(auth, dict) and auth.get("nonce"):
            return str(auth["nonce"])
        return None

    @staticmethod
    def _payer(sig: dict[str, Any]) -> str | None:
        payload = sig.get("payload")
        if not isinstance(payload, dict):
            return None
        auth = payload.get("authorization")
        if isinstance(auth, dict) and auth.get("from"):
            return str(auth["from"])
        return None

    def _push(self, facts: list[dict[str, Any]]) -> None:
        client = self.evidence or self.recorder
        try:
            client.push_facts(facts)
        except Exception as e:  # noqa: BLE001
            if not self.fail_open:
                raise
            log.warning("commitlayer x402: %d fact(s) not pushed: %s", len(facts), e)
