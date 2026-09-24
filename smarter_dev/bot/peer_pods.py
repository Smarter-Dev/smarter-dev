"""Ask Kubernetes whether any other bot pod is still running.

The acting lease covers processes that run the handover code. A predecessor
that never ran it (the first deploy of that code) or that died without handing
over cannot be seen in Redis, so before taking a free lease the bot checks
the pods themselves: it acts only once no other ``app=smarter-dev-bot`` pod
has a running container. Needs the Role in ``k8s/deploy-bot.yaml``.
"""

from __future__ import annotations

import logging
import os
import ssl
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
LABEL_SELECTOR = "app=smarter-dev-bot"
# If the API stays unreachable this long, act anyway: by then any predecessor
# has been past its 30s termination grace for a long time.
GIVE_UP_AFTER_SECONDS = 120.0

_failing_since: float | None = None


def running_elsewhere(pods: list[dict], me: str) -> list[str]:
    """Names of the other pods with a container still running."""
    names = []
    for pod in pods:
        name = pod.get("metadata", {}).get("name")
        if name == me:
            continue
        statuses = pod.get("status", {}).get("containerStatuses") or []
        if any("running" in (status.get("state") or {}) for status in statuses):
            names.append(name)
    return names


async def list_bot_pods() -> list[dict]:
    namespace = (SERVICE_ACCOUNT / "namespace").read_text().strip()
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods"
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url,
            params={"labelSelector": LABEL_SELECTOR},
            headers={"Authorization": f"Bearer {token}"},
            ssl=context,
        ) as response:
            response.raise_for_status()
            return (await response.json()).get("items", [])


async def predecessor_gone() -> bool:
    """True once no other bot pod runs; always True outside Kubernetes."""
    global _failing_since
    if "KUBERNETES_SERVICE_HOST" not in os.environ or not SERVICE_ACCOUNT.exists():
        return True
    try:
        others = running_elsewhere(await list_bot_pods(), os.environ.get("HOSTNAME", ""))
    except Exception as error:  # noqa: BLE001
        now = time.monotonic()
        _failing_since = _failing_since or now
        if now - _failing_since > GIVE_UP_AFTER_SECONDS:
            logger.error("cannot list bot pods for %.0fs (%s); acting anyway", now - _failing_since, error)
            return True
        logger.warning("cannot list bot pods yet: %s", error)
        return False
    _failing_since = None
    if others:
        logger.debug("waiting for bot pods to stop: %s", ", ".join(others))
    return not others
