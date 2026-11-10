"""CommitLayer for Temporal: record each mapped activity as an action, and gate completion on the verdict.

Temporal makes the workflow durable. CommitLayer independently verifies the result.

``temporalio`` is imported lazily: the mapping helpers work without it, the interceptor and the verifier need it
(``pip install commitlayer-integrations[temporal]``).
"""

from __future__ import annotations

from typing import Any

from .mapping import (
    CHANNEL,
    DEFAULT_ACTION_ID,
    ActivityMapping,
    MappingError,
    TemporalRef,
    bind_kwargs,
    build_action_body,
    resolve_path,
)

__all__ = [
    "CHANNEL",
    "DEFAULT_ACTION_ID",
    "ACTIVITY_NAME",
    "RECORD_FAILED",
    "ActivityMapping",
    "CommitLayerActivityInboundInterceptor",
    "CommitLayerInterceptor",
    "CommitLayerVerifier",
    "MappingError",
    "TemporalRef",
    "bind_kwargs",
    "build_action_body",
    "resolve_path",
]

_LAZY = {
    "CommitLayerInterceptor": "interceptor",
    "CommitLayerActivityInboundInterceptor": "interceptor",
    "RECORD_FAILED": "interceptor",
    "CommitLayerVerifier": "verifier",
    "ACTIVITY_NAME": "verifier",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        import importlib

        mod = importlib.import_module(f".{module}", __name__)
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise ImportError(
            f"{name} needs the Temporal SDK: pip install 'commitlayer-integrations[temporal]'"
        ) from e
    return getattr(mod, name)
