"""The fire payloads live apart from the jobs that consume them.

Everything that enqueues a fire — dispatch, the sweep, a script-armed timer,
the recurring chain — needs the payload and nothing else. Keeping the models
out of the job modules is what lets those callers import them without a cycle
through the job's own imports.
"""

from __future__ import annotations

import sys

from smarter_dev.web import admin_handlers_jobs
from smarter_dev.web import handlers_jobs
from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
from smarter_dev.web.handler_fire_payloads import HandlerFirePayload


def test_the_payload_module_pulls_in_neither_job():
    module = sys.modules["smarter_dev.web.handler_fire_payloads"]
    imported_by_it = {
        name
        for name, value in vars(module).items()
        if getattr(value, "__module__", "").startswith("smarter_dev.web.")
    }
    assert imported_by_it <= {"HandlerFirePayload", "AdminHandlerFirePayload"}


def test_an_omitted_depth_reads_as_a_chain_root():
    assert HandlerFirePayload(handler_id="h").chain_depth == 0
    assert AdminHandlerFirePayload(admin_handler_id="h").chain_depth == 0
    assert AdminHandlerFirePayload(admin_handler_id="h").channel_id == ""


def test_the_jobs_still_expose_their_payload_by_name():
    assert handlers_jobs.HandlerFirePayload is HandlerFirePayload
    assert admin_handlers_jobs.AdminHandlerFirePayload is AdminHandlerFirePayload
