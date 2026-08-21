"""Non-interactive Certora cloud authentication.

AutoProver runs headless — in a container, in CI, and inside long agentic
pipelines that submit prover jobs unattended. ``certora_login.login`` defaults to
completing a missing or stale session with the browser-based PKCE flow, which in
that setting cannot succeed: nobody opens the link, and the callback server waits
out its deadline before raising. A single prover call then costs five idle
minutes and still fails.

``login`` only reaches for the browser when the stored credentials could not be
refreshed, and it takes ``no_pkce`` to suppress that::

    credentials = get_credentials()
    if credentials:
        credentials = _who_am_i(credentials, ...)      # refresh / validate
    if not credentials and not resolved_no_pkce:
        credentials = pkce_login(...)                  # the browser flow
    if not credentials:
        raise CertoraLoginRefreshError(...)            # what we want instead

So the refresh path is unchanged and only the fallback differs: with the browser
ruled out, unusable credentials surface immediately as an error naming the fix.

The setting is applied as an environment default rather than an argument because
``ProverOutputAPI`` logs in on its own — its constructor calls
``get_auth_cookies`` → ``login(env=..., force_file=True)`` without passing
``no_pkce`` — and it logs in again, after deleting the stored credentials, when a
request comes back 401. Neither call is ours to pass arguments to, and both read
the same environment variable. ``setdefault`` leaves an operator who exports
``CERTORA_LOGIN_NO_PKCE=0`` in charge.
"""

import logging
import os
from functools import lru_cache

from certora_login import login
from prover_output_utility import ProverOutputAPI
from prover_output_utility.auth import resolve_login_env

_logger = logging.getLogger(__name__)

_LOGIN_HINT = (
    "No usable Certora cloud credentials. Install the public CLI "
    "(uv tool install certora-cloud) and run 'certora-cloud login' once on the "
    "host; it writes ~/.certora/credentials.json, which the container reads. "
    "Exporting CERTORA_USER/CERTORA_TOKEN/CERTORA_REFRESH_TOKEN works too."
)


class ProverAuthError(RuntimeError):
    """Certora cloud credentials are missing, or too stale to refresh."""


@lru_cache(maxsize=1)
def ensure_prover_login() -> None:
    """Refresh the cloud session, once per process, without a browser.

    Raises ``ProverAuthError`` when the credentials cannot be refreshed — a
    condition no amount of retrying fixes, since it needs a human to log in.

    Under ``CI`` this is a no-op, mirroring ProverOutputUtility's own
    precondition: ``get_auth_cookies`` returns an empty jar rather than logging
    in, and the API authenticates to Lambda with SigV4 instead. Our integration
    runner has AWS credentials and no credentials file, so insisting on a login
    that ProverOutputUtility will never perform would fail the job at this gate.
    """
    if os.getenv("CI"):
        return
    os.environ.setdefault("CERTORA_LOGIN_NO_PKCE", "1")
    try:
        login(env=resolve_login_env(), force_file=True)
    except Exception as exc:
        raise ProverAuthError(f"{_LOGIN_HINT}\nUnderlying error: {exc}") from exc
    _logger.info("Certora cloud credentials refreshed")


def prover_output_api(*, enable_cache: bool = True) -> ProverOutputAPI:
    """A ``ProverOutputAPI`` whose construction cannot open a browser.

    ``enable_cache`` mirrors ProverOutputUtility's own default so callers keep
    whatever they asked for.
    """
    ensure_prover_login()
    return ProverOutputAPI(enable_cache=enable_cache)
