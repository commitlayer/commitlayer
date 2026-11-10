"""CommitLayer for Inngest: record a step as an action, and gate completion on the verdict.

Inngest makes the function durable. CommitLayer independently verifies the result.

The helpers are duck-typed over the Inngest ``ctx`` / ``step`` objects, so this package imports without the
``inngest`` SDK; install ``commitlayer-integrations[inngest]`` for the SDK itself.
"""

from .steps import (
    CHANNEL,
    DEFAULT_ACTION_ID,
    CommitLayerSteps,
    RecordSpec,
    build_action_body,
    recorded_step,
    recorded_step_async,
    wait_for_verdict,
    wait_for_verdict_async,
)

__all__ = [
    "CHANNEL",
    "DEFAULT_ACTION_ID",
    "CommitLayerSteps",
    "RecordSpec",
    "build_action_body",
    "recorded_step",
    "recorded_step_async",
    "wait_for_verdict",
    "wait_for_verdict_async",
]
