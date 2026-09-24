"""Ask Kubernetes which other bot pods exist.

The acting lease covers processes that run the handover code. A predecessor
that never ran it (the first deploy of that code) or that died without handing
over cannot be seen in Redis, and a holder that lost Redis cannot see a
successor there. Both ask the pods themselves (``leadership.Coordinator``).

Any other ``app=smarter-dev-bot`` pod counts until it has Succeeded or Failed:
Pending (it may start any moment), Running, and Terminating (a legacy pod acts
until its container exits). A failed or timed-out call is an error, never an
answer: the caller treats it as "someone may be running". Needs the Role in
``k8s/deploy-bot.yaml``.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

import aiohttp

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
LABEL_SELECTOR = "app=smarter-dev-bot"
# Short enough that a holder checking every couple of seconds keeps a fresh
# answer (leadership.SOLE_POD_SECONDS).
REQUEST_TIMEOUT_SECONDS = 2.0

_FINISHED_PHASES = {"Succeeded", "Failed"}


def present_elsewhere(pods: list[dict], me: str) -> list[str]:
    """Names of the other bot pods that could be running a bot, in any phase
    but finished."""
    return [
        pod.get("metadata", {}).get("name", "?")
        for pod in pods
        if pod.get("metadata", {}).get("name") != me
        and pod.get("status", {}).get("phase") not in _FINISHED_PHASES
    ]


async def list_bot_pods() -> list[dict]:
    namespace = (SERVICE_ACCOUNT / "namespace").read_text().strip()
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods"
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            params={"labelSelector": LABEL_SELECTOR},
            headers={"Authorization": f"Bearer {token}"},
            ssl=context,
        ) as response:
            response.raise_for_status()
            return (await response.json()).get("items", [])


async def other_bot_pods() -> list[str]:
    """The other bot pods that exist now; raises if the API cannot say.

    Outside Kubernetes (local runs) there are none.
    """
    if "KUBERNETES_SERVICE_HOST" not in os.environ or not SERVICE_ACCOUNT.exists():
        return []
    return present_elsewhere(await list_bot_pods(), os.environ.get("HOSTNAME", ""))
