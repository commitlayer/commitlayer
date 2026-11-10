"""CommitLayer Python SDK: record agent actions, push facts, read verdicts, draft contracts.

Standard library only. One client per workspace API key::

    from commitlayer import Client

    cl = Client(api_key="spk_...")  # COMMITLAYER_API_KEY / COMMITLAYER_API_URL are read when omitted

    @cl.action("refund_workflow", subject=lambda order_id, **_: order_id, params=lambda amount, **_: {"amount": amount})
    def refund(order_id: str, amount: float) -> dict:
        ...  # the agent's own work, unchanged

The decorator records the action **before** the function runs, so the request time precedes every effect, and
never changes what the function returns. Recording failures are logged and swallowed by default
(``fail_open=True``): a verifier that is unreachable must not stop the business action, and the gap shows up as
an action that was never recorded rather than one that never happened. Set ``fail_open=False`` to raise.

Verification itself is done by the API from the systems of record; nothing here consults the function's result.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

__all__ = ["Client", "CommitLayerError", "__version__"]
__version__ = "0.1.0"

log = logging.getLogger("commitlayer")

Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]


class CommitLayerError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(f"{code} ({status}): {message}")
        self.status, self.code, self.message, self.details = status, code, message, details


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Client:
    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        *,
        timeout: float = 20.0,
        transport: Transport | None = None,
        agent_id: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("COMMITLAYER_API_KEY") or ""
        self.api_url = (
            api_url or os.environ.get("COMMITLAYER_API_URL") or "https://api.commitlayer.ai"
        ).rstrip("/")
        self.timeout = timeout
        self.agent_id = agent_id
        self._transport = transport or self._http
        if not self.api_key and transport is None:
            raise ValueError("api_key is required (or set COMMITLAYER_API_KEY)")

    # ---------------- transport ----------------

    def _http(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.api_url + path,
            data=data,
            method=method,
            headers={
                "X-API-Key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"commitlayer-python/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - fixed https host
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else {}
            except ValueError:
                return e.code, {
                    "error": {"code": "http_error", "message": raw.decode(errors="replace")[:300]}
                }

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        status, payload = self._transport(method, path, body)
        if status >= 400:
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                raise CommitLayerError(
                    status, err.get("code", "error"), err.get("message", ""), err.get("details")
                )
            raise CommitLayerError(status, "error", str(payload)[:300])
        return payload

    # ---------------- actions and facts ----------------

    def record_action(
        self,
        contract_id: str,
        subject_id: str,
        *,
        params: dict[str, Any] | None = None,
        action_id: str | None = None,
        action_type: str | None = None,
        subject_kind: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
        requested_at: str | None = None,
        channel: str | None = None,
        compensates: str | None = None,
        contract_version: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/actions``: record one action. ``compensates`` names an earlier action this one corrects;
        once this one settles PASS, that action is marked compensated (its own verdict never changes). Returns ``{action, verification, allowance}``. The verification
        is provisional until its settlement window closes and every source it reads has caught up."""
        body = {
            "action_id": action_id or f"act_{uuid.uuid4().hex}",
            "contract_id": contract_id,
            "subject_id": str(subject_id),
            "params": params or {},
            "requested_at": requested_at or _now(),
        }
        for k, v in (
            ("action_type", action_type),
            ("subject_kind", subject_kind),
            ("workflow_id", workflow_id),
            ("agent_id", agent_id or self.agent_id),
            ("channel", channel),
            ("compensates", compensates),
            ("contract_version", contract_version),
        ):
            if v is not None:
                body[k] = v
        return self._call("POST", "/v1/actions", body)

    def push_facts(
        self,
        facts: list[dict[str, Any]],
        *,
        links: list[dict[str, Any]] | None = None,
        watermarks: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/facts``. ``watermarks`` (source → time) states that everything up to that time has been sent
        for the source; settlement waits on it instead of the clock."""
        body: dict[str, Any] = {"facts": facts}
        if links:
            body["links"] = links
        if watermarks:
            body["watermarks"] = watermarks
        return self._call("POST", "/v1/facts", body)

    def verify(
        self,
        action_id: str | None = None,
        *,
        action: dict[str, Any] | None = None,
        facts: list[dict[str, Any]] | None = None,
        links: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """``POST /v1/actions/verify``: verify now, by id or inline. ``dry_run`` stores nothing."""
        body: dict[str, Any] = {"dry_run": dry_run}
        if action_id:
            body["action_id"] = action_id
        if action:
            body["action"] = action
        if facts:
            body["facts"] = facts
        if links:
            body["links"] = links
        return self._call("POST", "/v1/actions/verify", body)

    def get_action(self, action_ref: str) -> dict[str, Any]:
        return self._call("GET", f"/v1/actions/{action_ref}")

    def wait_for_settlement(self, action_ref: str, *, timeout: float = 300.0) -> dict[str, Any]:
        """The completion gate: block until the action's verdict settles or ``timeout`` seconds pass, and return
        the verification (``status`` is ``settled`` or, on timeout, ``pending``; ``timed_out`` says which). The
        API holds each call for up to 60 seconds; this loops. A workflow should not tell the customer "done"
        before this returns a settled PASS; FAIL routes to recovery; a timeout is the workflow's decision."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            budget = max(0.0, min(60.0, deadline - time.monotonic()))
            v = self._call("GET", f"/v1/actions/{action_ref}/wait?timeout={budget:.0f}")
            if not v.get("timed_out") or budget < 1 or time.monotonic() >= deadline:
                return v

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Any endpoint, with the client's auth and error handling."""
        return self._call(method, path, body)

    # ---------------- contracts ----------------

    def draft_contract(
        self,
        action_type: str | None = None,
        *,
        contract_id: str | None = None,
        lookback_days: int = 30,
        lookahead: str = "24h",
        min_share: float = 0.8,
    ) -> dict[str, Any]:
        """``POST /v1/contracts/draft``: a contract drafted from recorded actions and the facts around them.
        Returns ``{yaml, contract, errors, warnings, stats}``; nothing is saved."""
        return self._call(
            "POST",
            "/v1/contracts/draft",
            {
                "action_type": action_type,
                "contract_id": contract_id,
                "lookback_days": lookback_days,
                "lookahead": lookahead,
                "min_share": min_share,
            },
        )

    def validate_contract(self, yaml_text: str) -> dict[str, Any]:
        """``POST /v1/contracts/validate``: errors, compiled lenses and weakening warnings against the version
        it would replace."""
        return self._call("POST", "/v1/contracts/validate", {"yaml": yaml_text})

    def save_contract(self, yaml_text: str) -> dict[str, Any]:
        """``POST /v1/contracts``: save a version. The response carries ``warnings`` when it is weaker than the
        last one."""
        return self._call("POST", "/v1/contracts", {"yaml": yaml_text})

    # ---------------- decorator ----------------

    def action(
        self,
        contract_id: str,
        *,
        subject: Callable[..., Any] | str,
        params: Callable[..., dict[str, Any]] | None = None,
        action_type: str | None = None,
        subject_kind: str | None = None,
        workflow_id: Callable[..., str | None] | str | None = None,
        agent_id: str | None = None,
        action_id: Callable[..., str] | None = None,
        compensates: Callable[..., str | None] | str | None = None,
        fail_open: bool = True,
        on_recorded: Callable[[dict[str, Any]], None] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Record the decorated call as an action under ``contract_id`` before it runs.

        ``subject`` is the keyword argument name holding the subject id, or a callable over the bound arguments
        returning it. ``params`` is a callable over the bound arguments returning the parameters the contract
        limits (an amount, a tier). ``action_id`` makes retries idempotent: give it the same id and the API
        updates the one claim rather than recording two. The verification returned by the API is passed to
        ``on_recorded`` and stored on the function as ``last_recorded``.
        """
        import inspect

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            sig = inspect.signature(fn)

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    bound = sig.bind_partial(*args, **kwargs)
                    bound.apply_defaults()
                    ba = dict(bound.arguments)
                    subj = ba[subject] if isinstance(subject, str) else subject(**ba)
                    wf = workflow_id(**ba) if callable(workflow_id) else workflow_id
                    rec = self.record_action(
                        contract_id,
                        str(subj),
                        params=params(**ba) if params else {},
                        action_id=action_id(**ba) if action_id else None,
                        action_type=action_type,
                        subject_kind=subject_kind,
                        workflow_id=wf,
                        agent_id=agent_id,
                        compensates=compensates(**ba) if callable(compensates) else compensates,
                    )
                    wrapper.last_recorded = rec  # type: ignore[attr-defined]
                    if on_recorded:
                        on_recorded(rec)
                except Exception as e:  # noqa: BLE001 - the business action must not depend on the verifier
                    wrapper.last_recorded = None  # type: ignore[attr-defined]
                    if not fail_open:
                        raise
                    log.warning("commitlayer: action not recorded for %s: %s", fn.__name__, e)
                return fn(*args, **kwargs)

            wrapper.last_recorded = None  # type: ignore[attr-defined]
            return wrapper

        return deco
