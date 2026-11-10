"""A Temporal worker interceptor that records a CommitLayer action before a mapped activity runs.

Wire it once on the worker::

    from commitlayer import Client
    from commitlayer_temporal import CommitLayerInterceptor

    cl = Client()  # COMMITLAYER_API_KEY from the environment; never log it
    worker = Worker(
        client, task_queue="orders",
        workflows=[RefundWorkflow], activities=[refund_payment],
        interceptors=[CommitLayerInterceptor(cl, {
            "refund_payment": {
                "contract_id": "refund_workflow",
                "subject": "kwargs.order_id",
                "params": {"amount": "kwargs.amount", "reason": "kwargs.reason"},
                "subject_kind": "order",
            },
        }, agent_id="refund-agent")],
    )

The interceptor only declares the agent's intent: contract, subject and parameters, taken from the activity's
arguments before it executes. The activity's result is never sent. Verification reads the systems of record.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any

from commitlayer import Client
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

from .mapping import ActivityMapping, TemporalRef, bind_kwargs, build_action_body

__all__ = ["CommitLayerInterceptor", "CommitLayerActivityInboundInterceptor", "RECORD_FAILED"]

log = logging.getLogger("commitlayer.temporal")

RECORD_FAILED = "CommitLayerRecordFailed"
"""``ApplicationError.type`` raised when ``fail_open=False`` and the action could not be recorded."""


class CommitLayerInterceptor(Interceptor):
    """Record an action for every activity named in ``mapping``; leave the others alone.

    ``mapping`` is ``{activity name: ActivityMapping | dict}`` (see :mod:`commitlayer_temporal.mapping`).
    ``agent_id`` is sent on every action; ``workflow_id`` replaces the Temporal workflow id when set.
    With ``fail_open=True`` (default) a recording failure is logged as a warning and the activity still runs;
    with ``fail_open=False`` the activity does not run and a non-retryable ``ApplicationError`` (type
    ``CommitLayerRecordFailed``) fails it. ``on_recorded`` receives the API response for each recorded action.
    """

    def __init__(
        self,
        client: Client,
        mapping: Mapping[str, ActivityMapping | Mapping[str, Any]],
        *,
        agent_id: str | None = None,
        workflow_id: str | None = None,
        fail_open: bool = True,
        on_recorded: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.client = client
        self.mapping = {name: ActivityMapping.coerce(m) for name, m in mapping.items()}
        self.agent_id = agent_id or client.agent_id
        self.workflow_id = workflow_id
        self.fail_open = fail_open
        self.on_recorded = on_recorded

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return CommitLayerActivityInboundInterceptor(next, self)

    # ---- recording ----

    def body_for(self, input: ExecuteActivityInput, info: Any) -> dict[str, Any] | None:
        """The ``POST /v1/actions`` body for this invocation, or ``None`` when the activity is not mapped."""
        ref = TemporalRef.from_info(info)
        mapping = self.mapping.get(ref.activity_type)
        if mapping is None:
            return None
        return build_action_body(
            mapping,
            ref=ref,
            args=input.args,
            kwargs=bind_kwargs(input.fn, input.args),
            agent_id=self.agent_id,
            workflow_id=self.workflow_id,
        )

    def record(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /v1/actions`` with the body as built (the SDK's ``_call`` so ``contract_version`` is kept)."""
        return self.client._call("POST", "/v1/actions", body)


class CommitLayerActivityInboundInterceptor(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, config: CommitLayerInterceptor) -> None:
        super().__init__(next)
        self.config = config

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        try:
            body = self.config.body_for(input, info)
            if body is not None:
                # urllib blocks; keep the worker's event loop free while the action is recorded.
                rec = await asyncio.get_running_loop().run_in_executor(None, self.config.record, body)
                if self.config.on_recorded:
                    self.config.on_recorded(rec)
        except Exception as e:  # noqa: BLE001 - the business activity must not depend on the verifier
            if not self.config.fail_open:
                raise ApplicationError(
                    f"commitlayer: action not recorded for activity {info.activity_type}: {e}",
                    type=RECORD_FAILED,
                    non_retryable=True,
                ) from e
            log.warning(
                "commitlayer: action not recorded for activity %s (workflow %s): %s",
                info.activity_type,
                info.workflow_id,
                e,
            )
        return await self.next.execute_activity(input)
