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

A request the API answers with 429 or 503 **and** a ``Retry-After`` header is sent again after the wait the
header states: ``max_retries`` times at most (default 3, ``0`` turns it off), and only when the wait is no longer
than ``max_retry_wait`` seconds (default 10). No other answer is retried, and neither is a timeout or a network
error. See ``Client`` and the README.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import os
import posixpath
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

__all__ = ["Client", "CommitLayerError", "Packs", "__version__"]
__version__ = "0.1.3"  # kept equal to pyproject.toml; tests check it

log = logging.getLogger("commitlayer")

# A transport sends one request and returns ``(status, payload)`` or ``(status, payload, headers)``, where
# ``headers`` holds the response headers: a mapping, the headers object ``urllib`` and ``http.client`` give
# (``response.headers``), or a list of ``(name, value)`` pairs. A transport that returns no headers never causes
# a retry: the wait is read from ``Retry-After`` and from nowhere else.
Headers = Mapping[str, Any] | Iterable[tuple[Any, Any]]
Transport = Callable[
    [str, str, dict[str, Any] | None],
    tuple[int, dict[str, Any]] | tuple[int, dict[str, Any], Headers],
]

RETRY_STATUSES = (429, 503)
# The random addition to a wait is at most this many times the stated wait, so a burst refused together comes
# back spread over that time: a wait of one stated second is between one and three. The API admits a few writes
# of one workspace at a time and refuses the rest at once, so calls that return within the same fraction of a
# second are mostly refused together again; spread over two seconds they are admitted as they arrive.
_RETRY_SPREAD = 2.0
_DELTA_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]*)?|\.[0-9]+")


class CommitLayerError(RuntimeError):
    """An answer of 400 or above. ``retry_after`` is the wait its ``Retry-After`` header stated, in seconds, or
    ``None`` when it stated none the client could read; ``attempts`` is how many times the request was sent."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Any = None,
        *,
        retry_after: float | None = None,
        attempts: int = 1,
    ):
        super().__init__(f"{code} ({status}): {message}")
        self.status, self.code, self.message, self.details = status, code, message, details
        self.retry_after, self.attempts = retry_after, attempts

    def __reduce__(self) -> tuple[Any, ...]:
        # An exception is rebuilt from its ``args``, which here is the one formatted message; give the
        # constructor its own parameters back so the error can be pickled and copied (a process pool does both).
        return type(self), (self.status, self.code, self.message, self.details), dict(vars(self))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header_values(headers: Any, name: str) -> list[Any]:
    """Every value of one response header, whatever the case of its name. ``headers`` is a mapping, any object
    with ``items()`` (``http.client.HTTPMessage``, which can hold a name twice) or an iterable of
    ``(name, value)`` pairs; a name may be ``str`` or ``bytes``. Anything else is no headers at all."""
    if headers is None:
        return []
    found = []
    try:
        for key, value in headers.items() if hasattr(headers, "items") else headers:
            if isinstance(key, (bytes, bytearray)):
                key = key.decode("latin-1")
            if isinstance(key, str) and key.lower() == name:
                found.append(value)
    except (TypeError, ValueError):  # not headers: a number, a string, items that are not pairs
        return []
    return found


def _header(headers: Any, name: str) -> Any:
    """One response header, whatever the case of its name; ``None`` when absent or when there are no headers."""
    values = _header_values(headers, name)
    return values[0] if values else None


def _stated_wait(headers: Any, now: datetime | None = None) -> float | None:
    """The wait the answer's ``Retry-After`` states, in seconds, or ``None`` when it states none the client can
    read. An answer that carries the header twice (a proxy added its own) is taken at the longer wait."""
    waits = [w for w in (_parse_retry_after(v, now) for v in _header_values(headers, "retry-after")) if w is not None]
    return max(waits) if waits else None


def _parse_retry_after(value: Any, now: datetime | None = None) -> float | None:
    """The seconds a ``Retry-After`` header asks for: delta-seconds (an integer or a decimal, zero or more) or
    an HTTP-date, measured from ``now``; a date already past is zero. ``None`` when the header is absent or is
    anything else (negative, a word, an exponent, more digits than a number holds), which the client treats as
    no stated wait at all."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("latin-1")
    text = str(value).strip()
    if _DELTA_SECONDS.fullmatch(text):
        seconds = float(text)
        return seconds if math.isfinite(seconds) else None
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())
    except OverflowError:
        return None


def _repeatable(method: str, path: str, body: Any) -> bool:
    """Whether sending the request again can never record a second action. Recording is idempotent on
    ``action_id``, and the API may answer 503 ``busy`` to ``POST /v1/actions`` after the action itself is stored
    (its verification then runs in the background). A recording that names no ``action_id`` would be given a
    new one by the API on every attempt, so it is never sent twice. ``record_action`` always names one; a raw
    ``request`` and an inline ``verify`` carry whatever the caller wrote."""
    if method.upper() != "POST" or not isinstance(body, dict):
        return True
    # The route as the server will read it: without query or fragment, percent-decoded, and with ``//``,
    # ``/./`` and a trailing slash folded away, so another spelling of the same path is the same route.
    route = posixpath.normpath("/" + urllib.parse.unquote(re.split(r"[?#]", path, maxsplit=1)[0]).lstrip("/"))
    if route == "/v1/actions":
        return bool(body.get("action_id"))
    if route == "/v1/actions/verify" and isinstance(body.get("action"), dict) and not body.get("dry_run"):
        return bool(body["action"].get("action_id"))
    return True


class Client:
    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        *,
        timeout: float = 20.0,
        transport: Transport | None = None,
        agent_id: str | None = None,
        max_retries: int = 3,
        max_retry_wait: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        """``max_retries`` is how many times a request answered 429 or 503 with a ``Retry-After`` header is sent
        again (``0``: never). ``max_retry_wait`` is the longest stated wait, in seconds, the client accepts: a
        longer one is not waited and the error is raised at once. The waits of one call therefore add up to at
        most ``max_retries * max_retry_wait`` seconds. ``sleep`` does the waiting and ``jitter`` returns a number
        in ``[0, 1)`` that spreads the waits (``0`` is the stated wait, ``1`` three times it or the ceiling,
        whichever is less; anything else adds nothing); both exist for tests and for callers with their own
        clock."""
        if not (0 <= max_retries < math.inf and 0 <= max_retry_wait < math.inf):  # also false for NaN
            raise ValueError("max_retries and max_retry_wait must be numbers, zero or more")
        self.api_key = api_key or os.environ.get("COMMITLAYER_API_KEY") or ""
        self.api_url = (
            api_url or os.environ.get("COMMITLAYER_API_URL") or "https://api.commitlayer.ai"
        ).rstrip("/")
        self.timeout = timeout
        self.agent_id = agent_id
        self.max_retries = int(max_retries)
        self.max_retry_wait = float(max_retry_wait)
        self._sleep = sleep
        self._jitter = jitter
        self._transport = transport or self._http
        if not self.api_key and transport is None:
            raise ValueError("api_key is required (or set COMMITLAYER_API_KEY)")

    # ---------------- transport ----------------

    def _http(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
        """One request over ``urllib``. The headers come back as ``(name, value)`` pairs, every one of them: a
        name the answer carries twice is there twice."""
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
                return r.status, (json.loads(raw) if raw else {}), r.headers.items()
        except urllib.error.HTTPError as e:
            raw = e.read()
            headers = e.headers.items() if e.headers is not None else []
            try:
                return e.code, (json.loads(raw) if raw else {}), headers
            except ValueError:
                return (
                    e.code,
                    {"error": {"code": "http_error", "message": raw.decode(errors="replace")[:300]}},
                    headers,
                )

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send the request and return the payload, or raise ``CommitLayerError`` for an answer of 400 or above.

        An answer of 429 or 503 that carries a ``Retry-After`` no longer than ``max_retry_wait`` is waited out
        and the same request, with the same body, is sent again, ``max_retries`` times at most; after that the
        last answer is the error. Nothing else is retried: not another status, not a 429 or 503 without the
        header, and not a timeout or a network error, after which nobody knows whether the request arrived."""
        attempts = 0
        while True:
            attempts += 1
            answer = self._transport(method, path, body)
            status, payload = answer[0], answer[1]
            if status < 400:
                return payload
            retry_after = _stated_wait(answer[2] if len(answer) > 2 else None)
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                error = CommitLayerError(
                    status,
                    err.get("code", "error"),
                    err.get("message", ""),
                    err.get("details"),
                    retry_after=retry_after,
                    attempts=attempts,
                )
            else:
                error = CommitLayerError(
                    status, "error", str(payload)[:300], retry_after=retry_after, attempts=attempts
                )
            if (
                attempts > self.max_retries
                or status not in RETRY_STATUSES
                or retry_after is None
                or retry_after > self.max_retry_wait
                or not _repeatable(method, path, body)
            ):
                raise error
            # Never less than the server stated and never more than the ceiling, so the bound holds exactly: the
            # addition is a random share of twice the stated wait, or of what is left under the ceiling when
            # that is less.
            try:
                spread = float(self._jitter())
            except (TypeError, ValueError):
                spread = 0.0
            room = min(retry_after * _RETRY_SPREAD, self.max_retry_wait - retry_after)
            wait = retry_after + (room * spread if 0.0 <= spread <= 1.0 else 0.0)
            log.info(
                "commitlayer: %s %s answered %s %s; sending it again in %.2f s (retry %d of %d)",
                method,
                path.split("?", 1)[0],
                status,
                error.code,
                wait,
                attempts,
                self.max_retries,
            )
            self._sleep(wait)

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
        """``POST /v1/actions/verify``: verify now, by id or inline. ``dry_run`` stores nothing. An inline
        ``action`` that is stored and names no ``action_id`` is sent once only, never retried."""
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
        before this returns a settled PASS; FAIL routes to recovery; a timeout is the workflow's decision.
        A poll the API refuses with ``Retry-After`` is retried like any other request, which can add up to
        ``max_retries * max_retry_wait`` seconds to ``timeout``."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            budget = max(0.0, min(60.0, deadline - time.monotonic()))
            v = self._call("GET", f"/v1/actions/{action_ref}/wait?timeout={budget:.0f}")
            if not v.get("timed_out") or budget < 1 or time.monotonic() >= deadline:
                return v

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Any endpoint, with the client's auth, error handling and retries. A ``POST /v1/actions`` body
        without an ``action_id`` is sent once only: the API would record a new action on every attempt."""
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

    # ---------------- workflow packs ----------------

    @property
    def packs(self) -> Packs:
        """The workflow packs (``/v1/packs``): ``client.packs.list()``, ``get``, ``configure``, ``dry_run``,
        ``install``. Install creates drafts and never activates; activation stays a person's
        ``POST /v1/contracts/{id}/select``."""
        return Packs(self)

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


class Packs:
    """``/v1/packs``: the workflow pack library. Reads take any member; ``configure`` and ``dry_run`` write nothing
    and take an analyst; ``install`` saves the configured contracts as **drafts** (never activates) and needs
    ``confirm=True``. ``configuration`` is ``{workflow_id: {parameter_key: value}}``."""

    def __init__(self, client: Client):
        self._c = client

    def list(self) -> dict[str, Any]:
        """``GET /v1/packs``: ``{items: PackSummary[]}``."""
        return self._c._call("GET", "/v1/packs")

    def get(self, pack_id: str) -> dict[str, Any]:
        """``GET /v1/packs/{id}``: the summary plus contracts, recorder mapping, fixtures, templates, readme,
        versions and the workspace's requirements (plan, connectors, evidence)."""
        return self._c._call("GET", f"/v1/packs/{pack_id}")

    def configure(self, pack_id: str, configuration: dict[str, Any] | None = None) -> dict[str, Any]:
        """``POST /v1/packs/{id}/configure``: the contracts with the configuration applied, the values in force and
        every way a value weakens a check. Nothing is written."""
        return self._c._call("POST", f"/v1/packs/{pack_id}/configure", {"configuration": configuration or {}})

    def dry_run(
        self, pack_id: str, configuration: dict[str, Any] | None = None, *, workflow: str | None = None
    ) -> dict[str, Any]:
        """``POST /v1/packs/{id}/dry-run``: every fixture case through the engine against the configured
        contracts, in a savepoint; ``{summary: {total, ok}, cases: [...]}``. Nothing persists."""
        body: dict[str, Any] = {"configuration": configuration or {}}
        if workflow:
            body["workflow"] = workflow
        return self._c._call("POST", f"/v1/packs/{pack_id}/dry-run", body)

    def install(
        self, pack_id: str, configuration: dict[str, Any] | None = None, *, confirm: bool = False
    ) -> dict[str, Any]:
        """``POST /v1/packs/{id}/install``: save the configured contracts as drafts and record the installation.
        Refused without ``confirm=True``; answers 402 ``plan_required`` below the pack's minimum plan. Returns
        the drafts' versions, the recorder mapping with those versions pinned and the activation steps."""
        return self._c._call(
            "POST", f"/v1/packs/{pack_id}/install", {"configuration": configuration or {}, "confirm": confirm}
        )

    def coverage(self, pack_id: str) -> dict[str, Any]:
        """``GET /v1/packs/{id}/coverage``: sources covered and their trust, unmapped consequential tools,
        activation state per contract, identity links seen."""
        return self._c._call("GET", f"/v1/packs/{pack_id}/coverage")

    def recorder_mapping(self, pack_id: str) -> dict[str, Any]:
        """``GET /v1/packs/{id}/recorder-mapping``: the installed mapping, contract versions pinned."""
        return self._c._call("GET", f"/v1/packs/{pack_id}/recorder-mapping")
