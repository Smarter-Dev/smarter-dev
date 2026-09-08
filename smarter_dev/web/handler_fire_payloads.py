"""The job payloads that enqueue one handler firing, per tier.

Everything that enqueues a fire needs these and nothing else: event dispatch,
the stalled-schedule sweep, a script-armed timer, the recurring chain. They live
apart from the fire jobs so those callers can import a payload without pulling
in the job module and whatever it imports.

Importing a payload registers nothing: ``submit()`` resolves a payload to its
job only once the job module has been imported, which every submitting process
does at its entry point via ``worker_imports.import_worker_job_modules``.
"""

from __future__ import annotations

from pydantic import BaseModel


class HandlerFirePayload(BaseModel):
    """Job payload for one member-handler firing."""

    handler_id: str
    trigger_context: dict = {}
    # How many handler fires deep this fire is (0 = caused by a gateway event).
    # An explicit FIELD, never a trigger_context key: context goes to the sandbox
    # verbatim, so a depth in there would be script-readable and script-forgeable.
    # Defaulted so an omitted field means "chain root", not a crash — schedule
    # re-arms and any older enqueued job read as roots, which is what they are.
    chain_depth: int = 0


class AdminHandlerFirePayload(BaseModel):
    """Job payload for one admin-handler firing."""

    admin_handler_id: str
    channel_id: str = ""
    trigger_context: dict = {}
    chain_depth: int = 0
