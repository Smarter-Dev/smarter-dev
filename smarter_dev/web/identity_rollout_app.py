"""Deployment-only ASGI wrapper; all requests enter through the admission gate."""

from main import app as application
from smarter_dev.web.identity_admission import IdentityAdmissionGate

app = IdentityAdmissionGate(application)
