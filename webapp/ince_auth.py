"""Shared-secret HTTP Basic auth for the INCE Flask services.

WHY THIS EXISTS

Every one of these tools was written to bind 127.0.0.1 on the Mac mini. That
loopback bind is the only thing that has ever protected them: none of them has
a login, and between them they serve portfolio company financials, deal memos,
declined-deal records and fundraising history.

Deploying to Railway removes that protection. Each service gets a public HTTPS
URL, so the loopback assumption silently becomes "readable by anyone who finds
the hostname". This module is the smallest thing that closes that hole.

There is deliberately no per-user identity here. A single shared secret is not
good access control -- it cannot be revoked for one person and it leaves no
audit trail -- but it is the difference between "confidential" and "public",
and it needs no new infrastructure. Real accounts belong to whatever SSO fronts
the deployment later, not to this file.

USAGE

    from ince_auth import install_auth

    app = Flask(__name__)
    install_auth(app, realm="Fundraising History")

That registers a before_request hook covering every route, plus an
unauthenticated /healthz for the platform probe.

CONFIGURATION

    INCE_AUTH_PASSWORD   The shared secret. One value across all services, so
                         a single Railway shared variable protects the fleet.
    INCE_AUTH_USER       Username. Defaults to "ince".
    INCE_REQUIRE_AUTH=1  Refuse to start if the password is unset. The
                         Dockerfiles set this, so a misconfigured deploy fails
                         loudly at boot instead of serving documents wide open.

Leaving INCE_AUTH_PASSWORD unset disables auth entirely. That is intentional
and is what local `python app.py` runs do -- it is safe there and only there,
because the app binds loopback. It is why INCE_REQUIRE_AUTH exists: in a
container, "no password" must be a crash, not a default.
"""

from __future__ import annotations

import hmac
import os

from flask import Response, request

# Read once at import. Rotating the secret means redeploying the service, which
# is the correct blast radius -- a value re-read per request could change under
# a running process and make "am I locked out?" depend on timing.
AUTH_USER = os.environ.get("INCE_AUTH_USER", "ince")
AUTH_PASSWORD = os.environ.get("INCE_AUTH_PASSWORD", "")
REQUIRE_AUTH = os.environ.get("INCE_REQUIRE_AUTH") == "1"

# Paths that never require credentials. Health probes carry none, and a probe
# against an authenticated route would 401 and the deploy would never go
# healthy. /_stcore/health is Streamlit's equivalent, harmless to allow here.
PUBLIC_PATHS = frozenset({"/healthz", "/_stcore/health"})

if REQUIRE_AUTH and not AUTH_PASSWORD:
    raise RuntimeError(
        "INCE_REQUIRE_AUTH=1 but INCE_AUTH_PASSWORD is empty. Set "
        "INCE_AUTH_PASSWORD to a strong secret in the Railway service "
        "variables, or unset INCE_REQUIRE_AUTH for a loopback-only local run."
    )


def credentials_ok(username: str | None, password: str | None) -> bool:
    """Constant-time credential check.

    compare_digest on both halves rather than ==: a plain comparison leaks
    length and common-prefix information through timing, and this secret is the
    only thing standing in front of confidential documents. Both calls always
    run, so the check does not short-circuit on a wrong username either.
    """
    user_ok = hmac.compare_digest(username or "", AUTH_USER)
    password_ok = hmac.compare_digest(password or "", AUTH_PASSWORD)
    return user_ok and password_ok


def install_auth(app, realm: str = "INCE") -> None:
    """Gate every route on the shared secret and add /healthz.

    No-op for authentication when INCE_AUTH_PASSWORD is unset -- see the module
    docstring for why that is safe locally and impossible in a container.
    """

    @app.before_request
    def _require_auth():
        if not AUTH_PASSWORD:
            return None  # local loopback run
        if request.path in PUBLIC_PATHS:
            return None
        auth = request.authorization
        if auth is not None and auth.type == "basic" and credentials_ok(
            auth.username, auth.password
        ):
            return None
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": f'Basic realm="{realm}"'},
        )

    # add_url_rule rather than @app.route so this is idempotent: a service that
    # already defines /healthz (Portfolio-Review does) keeps its own and does
    # not blow up on a duplicate endpoint name at import time.
    if "healthz" not in app.view_functions:
        app.add_url_rule("/healthz", "healthz", _healthz)


def _healthz():
    """Unauthenticated liveness probe.

    Returns a bare "ok" -- no version, no config, no job state -- so it leaks
    nothing to an unauthenticated caller.
    """
    return Response("ok", mimetype="text/plain")
