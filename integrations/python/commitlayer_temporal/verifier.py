"""The workflow completion gate: an activity that waits for a CommitLayer verdict to settle.

Each poll goes through the API's completion gate (``GET /v1/actions/{ref}/wait``), which holds the request for up
to ``poll_seconds`` and returns the moment the verdict settles; the loop around it lives in
:func:`commitlayer_integrations.verdict.poll_until_settled` and this module wraps it as a Temporal activity::

    verifier = CommitLayerVerifier(Client())
    Worker(..., activities=[refund_payment, verifier.commitlayer_verify], activity_executor=ThreadPoolExecutor(8))

and, in the workflow, after the activities whose actions were recorded::

    verdict = await workflow.execute_activity(
        CommitLayerVerifier.commitlayer_verify,
        args=["temporal:" + workflow.info().workflow_id + ":refund", 900],
        start_to_close_timeout=timedelta(minutes=20),
        heartbeat_timeout=timedelta(minutes=1),
    )
    if verdict["passed"]:
        ...  # only now tell the customer it is done
    elif verdict["status"] == "settled":
        ...  # FAIL: run the compensation activity (its action should carry compensates=<this action id>)
    else:
        ...  # timed out while pending: the workflow decides (wait longer, escalate, or hold the confirmation)

The activity is synchronous (the SDK's transport is ``urllib``), so the worker needs an ``activity_executor``.
It heartbeats after every poll. A cancellation reaches a sync activity as ``CancelledError`` raised in its
thread (Temporal's default); between polls the loop also checks ``activity.is_cancelled()`` and raises it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from commitlayer import Client
from commitlayer_integrations.verdict import Verdict, fetch_verification, poll_until_settled
from temporalio import activity
from temporalio.exceptions import CancelledError

__all__ = ["CommitLayerVerifier", "ACTIVITY_NAME"]

log = logging.getLogger("commitlayer.temporal")

ACTIVITY_NAME = "commitlayer_verify"


class CommitLayerVerifier:
    """Holds the client; ``commitlayer_verify`` is the activity to register on the worker.

    ``default_timeout_seconds`` and ``poll_seconds`` apply when the workflow does not pass its own.
    ``sleep`` and ``clock`` exist for tests.
    """

    def __init__(
        self,
        client: Client,
        *,
        default_timeout_seconds: float = 900.0,
        poll_seconds: float = 5.0,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # The gate holds each poll server-side, so no local sleep is needed between polls; tests inject one.
        self.client = client
        self.default_timeout_seconds = default_timeout_seconds
        self.poll_seconds = poll_seconds
        self._sleep = sleep or (lambda s: None)
        self._clock = clock

    @activity.defn(name=ACTIVITY_NAME)
    def commitlayer_verify(
        self,
        action_ref: str,
        timeout_seconds: float | None = None,
        poll_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Wait on the completion gate (``GET /v1/actions/{action_ref}/wait``) until the verification settles
        or the timeout passes; each call to the gate is held for at most ``poll_seconds`` and a heartbeat follows.

        Returns a :class:`commitlayer_integrations.verdict.Verdict`: ``passed`` is true only for a settled PASS
        under both lenses; ``timed_out`` is true when the deadline passed while the verdict was still
        ``pending`` (or never verified). Both are JSON, so the workflow can branch on them and Temporal can
        store them in history.
        """
        timeout = self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        poll = self.poll_seconds if poll_seconds is None else float(poll_seconds)

        def heartbeat(v: Verdict) -> None:
            activity.heartbeat({"status": v["status"], "outcome": v["outcome"], "policy": v["policy"]})

        verdict = poll_until_settled(
            lambda: fetch_verification(self.client, action_ref, poll),
            timeout_seconds=timeout,
            poll_seconds=poll,
            sleep=self._sleep,
            clock=self._clock,
            on_poll=heartbeat,
            should_stop=activity.is_cancelled,
        )
        if verdict["timed_out"] and activity.is_cancelled():
            raise CancelledError(f"commitlayer_verify cancelled while {action_ref} was {verdict['status']}")
        if verdict["timed_out"]:
            log.warning(
                "commitlayer: verdict for %s still %s after %.0fs", action_ref, verdict["status"], timeout
            )
        return dict(verdict)
