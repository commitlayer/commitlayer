"""From a Temporal activity invocation to the body of ``POST /v1/actions``.

Pure: nothing here imports ``temporalio`` or talks to the network, so the exact body an activity produces can
be asserted in a unit test. The interceptor in :mod:`commitlayer_temporal.interceptor` calls
:func:`build_action_body` and posts the result unchanged.

Paths
-----
A mapping names where the subject and the parameters live in the activity's arguments:

* ``args.0.order_id`` -- the first positional argument's ``order_id`` (dict key or attribute);
* ``kwargs.order_id`` -- the argument bound to the activity function's parameter ``order_id``;
* ``order_id`` -- shorthand for ``kwargs.order_id``.

Temporal delivers activity arguments positionally; ``kwargs.<name>`` works because the integration binds the
arguments to the activity function's signature, so a mapping can use parameter names instead of positions.

Templates
---------
``action_id`` (and nothing else) is a template over ``{workflow_id}``, ``{workflow_run_id}``, ``{activity_id}``,
``{attempt}``, ``{activity}``, ``{subject_id}`` and every resolved parameter by name (``{amount}``). Without a
template the action id is ``temporal:{workflow_id}:{activity_id}``: every retry attempt of one activity
execution records the one action, because the API is idempotent on ``action_id``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

CHANNEL = "temporal"
DEFAULT_ACTION_ID = "temporal:{workflow_id}:{activity_id}"

__all__ = [
    "CHANNEL",
    "DEFAULT_ACTION_ID",
    "ActivityMapping",
    "MappingError",
    "TemporalRef",
    "bind_kwargs",
    "build_action_body",
    "resolve_path",
]


class MappingError(LookupError):
    """A path or template in an activity mapping could not be resolved against the invocation."""


@dataclass(frozen=True)
class ActivityMapping:
    """What to record for one activity name.

    ``subject`` and every value of ``params`` and ``compensates`` are paths (see the module docstring);
    ``action_id`` is a template. ``contract_version`` pins the version the action is recorded under.
    """

    contract_id: str
    subject: str
    params: Mapping[str, str] = field(default_factory=dict)
    action_id: str | None = None
    action_type: str | None = None
    subject_kind: str | None = None
    compensates: str | None = None
    contract_version: int | None = None

    @classmethod
    def coerce(cls, value: ActivityMapping | Mapping[str, Any]) -> ActivityMapping:
        if isinstance(value, ActivityMapping):
            return value
        unknown = set(value) - {f for f in cls.__dataclass_fields__}
        if unknown:
            raise MappingError(f"unknown mapping keys: {sorted(unknown)}")
        return cls(**dict(value))


@dataclass(frozen=True)
class TemporalRef:
    """The Temporal identifiers an action carries; ``from_info`` reads them off ``temporalio.activity.Info``."""

    workflow_id: str | None
    activity_id: str
    attempt: int
    activity_type: str
    workflow_run_id: str | None = None

    @classmethod
    def from_info(cls, info: Any) -> TemporalRef:
        return cls(
            workflow_id=getattr(info, "workflow_id", None),
            activity_id=str(getattr(info, "activity_id", "")),
            attempt=int(getattr(info, "attempt", 1)),
            activity_type=str(getattr(info, "activity_type", "")),
            workflow_run_id=getattr(info, "workflow_run_id", None),
        )


def bind_kwargs(fn: Callable[..., Any], args: Sequence[Any]) -> dict[str, Any]:
    """The activity's positional arguments by parameter name (``self`` excluded for bound methods)."""
    try:
        bound = inspect.signature(fn).bind_partial(*args)
    except (TypeError, ValueError):
        return {}
    bound.apply_defaults()
    return dict(bound.arguments)


def _step(value: Any, key: str, path: str) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
        raise MappingError(f"{path!r}: no key {key!r}")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        try:
            return value[int(key)]
        except (ValueError, IndexError) as e:
            raise MappingError(f"{path!r}: no index {key!r}") from e
    if hasattr(value, key):
        return getattr(value, key)
    raise MappingError(f"{path!r}: {type(value).__name__} has no attribute {key!r}")


def resolve_path(path: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
    """Resolve ``args.N.x.y`` / ``kwargs.x.y`` / ``x.y`` (= ``kwargs.x.y``) against an invocation."""
    if not isinstance(path, str) or not path:
        raise MappingError(f"path must be a non-empty string, got {path!r}")
    parts = path.split(".")
    root, rest = parts[0], parts[1:]
    if root == "args":
        value: Any = list(args)
    elif root == "kwargs":
        value = dict(kwargs)
    else:
        value, rest = dict(kwargs), parts
    for key in rest:
        value = _step(value, key, path)
    return value


class _Fields(dict):
    def __missing__(self, key: str) -> str:
        raise MappingError(f"action_id template: unknown placeholder {{{key}}}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017 - Python 3.10


def build_action_body(
    mapping: ActivityMapping | Mapping[str, Any],
    *,
    ref: TemporalRef,
    args: Sequence[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
    agent_id: str | None = None,
    workflow_id: str | None = None,
    requested_at: str | None = None,
) -> dict[str, Any]:
    """The exact ``POST /v1/actions`` body for one activity invocation.

    ``workflow_id`` overrides the Temporal workflow id as the action's ``workflow_id``. ``requested_at``
    defaults to now, which is before the activity runs, so the request time precedes every effect.
    """
    m = ActivityMapping.coerce(mapping)
    kw = dict(kwargs or {})
    subject_id = resolve_path(m.subject, args, kw)
    if subject_id is None or subject_id == "":
        raise MappingError(f"subject path {m.subject!r} resolved to nothing")
    params: dict[str, Any] = {name: resolve_path(path, args, kw) for name, path in m.params.items()}
    fields = _Fields(
        params,
        subject_id=str(subject_id),
        workflow_id=ref.workflow_id or "",
        workflow_run_id=ref.workflow_run_id or "",
        activity_id=ref.activity_id,
        attempt=ref.attempt,
        activity=ref.activity_type,
    )
    action_id = (m.action_id or DEFAULT_ACTION_ID).format_map(fields)
    if not action_id or len(action_id) > 120:
        raise MappingError(f"action_id {action_id!r} must be 1-120 characters")

    body: dict[str, Any] = {
        "action_id": action_id,
        "contract_id": m.contract_id,
        "workflow_id": workflow_id or ref.workflow_id,
        "subject_id": str(subject_id),
        "requested_at": requested_at or _now(),
        "params": {**params, "temporal_activity_id": ref.activity_id, "temporal_attempt": ref.attempt},
        "channel": CHANNEL,
    }
    optional = (
        ("agent_id", agent_id),
        ("action_type", m.action_type),
        ("subject_kind", m.subject_kind),
        ("compensates", resolve_path(m.compensates, args, kw) if m.compensates else None),
        ("contract_version", m.contract_version),
    )
    for key, value in optional:
        if value is not None:
            body[key] = value
    if body["workflow_id"] is None:
        del body["workflow_id"]
    return body
