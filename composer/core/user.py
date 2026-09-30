import os

#: The environment variable that names the tenant a run works for. The cloud
#: sets it on every job; a local run leaves it unset.
USER_ID_ENV = "AUTOPROVER_USER_ID"

#: The tenant a run without ``USER_ID_ENV`` works for.
ANONYMOUS_UID = "_anonymous"


def get_uid_or_none() -> str | None:
    """The tenant named by ``USER_ID_ENV``, or ``None`` when it is unset or
    blank. For callers that must refuse to run without a tenant rather than
    fall back to the anonymous one."""
    return os.environ.get(USER_ID_ENV) or None


def get_uid() -> str:
    return get_uid_or_none() or ANONYMOUS_UID


def user_data_ns(uid: str | None = None) -> tuple[str, ...]:
    """Conventional namespace prefix for tenant-scoped data.

    Single source of truth: any per-user content (CVL research write
    layer, source-code-agent caches, etc.) is stored under
    ``("user_data", uid, …)``. Future refactors of the convention
    (per-org scope, per-engagement scope) only need to touch this
    function.
    """
    uid = get_uid() if not uid else uid
    return ("user_data", uid)
