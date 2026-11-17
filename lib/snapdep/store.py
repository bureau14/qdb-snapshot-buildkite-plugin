"""Object store access: the ``Store`` interface, its error policy, and S3/R2.

Keys are relative to the artifact destination (bucket plus optional prefix), so
one store reads both the snapdep records (under ``_snapdep/v1/``, see
``snapdep.records``) and the artifacts plugin's ``LATEST_SUCCESSFUL`` pointers.

Error policy (specification section 4):

- 429 and 5xx answers and connection failures are retried with backoff;
- a missing object is an answer (``get`` returns None);
- 412 on a conditional put is an answer (``put`` returns False);
- anything else, or a transient failure that outlives the retries, raises
  ``StoreError``. A failed lookup is never read as "no record".

Measured on the real bucket: a conditional PutObject is atomic, and the losers
of a create race get 412 or sometimes 429, which a retry turns into 412.
Conditions on copy and delete are ignored by the store, so this interface
offers neither.

Configuration and credentials come from the same sources, in the same order,
as the artifacts plugin (its ``lib/artifacts.py``, ``load_store_config`` and
``resolve_object_auth``): ``ARTIFACTS_*`` environment variables, then SSM
parameters, then defaults.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar

T = TypeVar("T")


class StoreError(RuntimeError):
    """A store operation failed and its answer is unknown."""


class StoreConfigError(StoreError):
    """The store is not configured, or is configured with a placeholder."""


@dataclass(frozen=True)
class StoredObject:
    """An object's body and its ETag (as returned by the store, quotes included)."""

    body: bytes
    etag: str


class Store(Protocol):
    """What snapdep may do to the store. Keys never start with "/"."""

    def get(self, key: str) -> StoredObject | None:
        """The object at ``key``, or None when it does not exist."""
        ...

    def put(
        self, key: str, body: bytes, *, if_none_match: bool = False, if_match: str | None = None
    ) -> bool:
        """Write ``body`` at ``key``.

        ``if_none_match``: only when ``key`` does not exist (create once).
        ``if_match``: only when the current ETag equals this one (compare and swap).
        At most one of the two. Returns False when the condition did not hold.
        """
        ...

    def list_keys(self, prefix: str) -> list[str]:
        """Every key that starts with ``prefix``, sorted."""
        ...

    def list_children(self, prefix: str) -> list[str]:
        """The names of the immediate "directories" under ``prefix`` (which ends in "/").

        For keys ``a/x/1`` and ``a/y/2``, ``list_children("a/")`` is ``["x", "y"]``.
        """
        ...


def key_join(*parts: str) -> str:
    """Join key segments with "/", dropping empty segments and stray slashes."""
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


def check_conditions(if_none_match: bool, if_match: str | None) -> None:
    """Reject a put that asks for both conditions."""
    if if_none_match and if_match is not None:
        raise ValueError("put: if_none_match and if_match are mutually exclusive")


# ---------------------------------------------------------------------------
# Error policy
# ---------------------------------------------------------------------------

RETRY = "retry"
ABSENT = "absent"
PRECONDITION = "precondition"
FATAL = "fatal"

_RETRY_CODES = {
    "SlowDown",
    "TooManyRequests",
    "Throttling",
    "ThrottlingException",
    "RequestTimeout",
    "InternalError",
    "ServiceUnavailable",
}
_ABSENT_CODES = {"NoSuchKey", "NotFound", "404"}
_PRECONDITION_CODES = {"PreconditionFailed", "412"}


def _transient_types() -> tuple[type, ...]:
    from botocore import exceptions as e

    names = (
        "ConnectionError",
        "HTTPClientError",
        "IncompleteReadError",
        "ResponseStreamingError",
    )
    return tuple(getattr(e, n) for n in names if hasattr(e, n))


def classify(exc: BaseException) -> str:
    """Map an exception of a store call to RETRY, ABSENT, PRECONDITION or FATAL."""
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = str(err.get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        if status == 412 or code in _PRECONDITION_CODES:
            return PRECONDITION
        if status == 404 or code in _ABSENT_CODES:
            return ABSENT
        if status == 429 or status >= 500 or code in _RETRY_CODES:
            return RETRY
        return FATAL
    if isinstance(exc, _transient_types()):
        return RETRY
    return FATAL


@dataclass(frozen=True)
class RetryPolicy:
    """Backoff schedule: ``base`` seconds doubling up to ``cap``, ``attempts`` in total."""

    attempts: int = 8
    base: float = 1.0
    cap: float = 30.0

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        return min(self.base * (2 ** (attempt - 1)), self.cap)


def with_retries(
    op: Callable[[], T],
    describe: str,
    policy: RetryPolicy,
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> T:
    """Run ``op`` until it returns or raises a non-RETRY exception.

    Exceptions classified RETRY are retried per ``policy``; the last one becomes
    a ``StoreError``. Other exceptions propagate unchanged for the caller to
    classify.
    """
    for attempt in range(1, policy.attempts + 1):
        try:
            return op()
        except Exception as exc:
            if classify(exc) != RETRY:
                raise
            if attempt == policy.attempts:
                raise StoreError(f"{describe}: failed after {attempt} attempts: {exc}") from exc
            d = policy.delay(attempt)
            log(f"{describe}: attempt {attempt} failed ({exc}); retrying in {d:.0f}s")
            sleep(d)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# S3 / R2 implementation
# ---------------------------------------------------------------------------


def _log_stderr(msg: str) -> None:
    import sys

    print(f"[snapdep] {msg}", file=sys.stderr)


class S3Store:
    """``Store`` on an S3-compatible client (boto3), under ``prefix`` of ``bucket``.

    ``client`` is a boto3 S3 client with its own retries switched off; the
    retries of this class follow the error policy of the module docstring.
    """

    def __init__(
        self,
        client: Any,
        bucket: str,
        prefix: str = "",
        policy: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = _log_stderr,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._policy = policy
        self._sleep = sleep
        self._log = log

    def _full(self, key: str) -> str:
        return key_join(self._prefix, key)

    def _relative(self, full: str) -> str:
        return full[len(self._prefix) + 1 :] if self._prefix else full

    def _retrying(self, op: Callable[[], T], describe: str) -> T:
        return with_retries(op, describe, self._policy, self._sleep, self._log)

    def get(self, key: str) -> StoredObject | None:
        full = self._full(key)

        def op() -> StoredObject:
            r = self._client.get_object(Bucket=self._bucket, Key=full)
            return StoredObject(body=r["Body"].read(), etag=r["ETag"])

        try:
            return self._retrying(op, f"get {full}")
        except StoreError:
            raise
        except Exception as exc:
            if classify(exc) == ABSENT:
                return None
            raise StoreError(f"get {full}: {exc}") from exc

    def put(
        self, key: str, body: bytes, *, if_none_match: bool = False, if_match: str | None = None
    ) -> bool:
        check_conditions(if_none_match, if_match)
        full = self._full(key)
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": full,
            "Body": body,
            "ContentType": "application/json",
        }
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        try:
            self._retrying(lambda: self._client.put_object(**kwargs), f"put {full}")
            return True
        except StoreError:
            raise
        except Exception as exc:
            if classify(exc) == PRECONDITION and (if_none_match or if_match is not None):
                return False
            raise StoreError(f"put {full}: {exc}") from exc

    def _list_pages(self, prefix: str, delimiter: str | None) -> list[dict]:
        full = self._full(prefix) + ("/" if prefix.endswith("/") else "")
        pages: list[dict] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": full}
            if delimiter:
                kwargs["Delimiter"] = delimiter
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = self._retrying(
                    lambda kw=kwargs: self._client.list_objects_v2(**kw), f"list {full}"
                )
            except StoreError:
                raise
            except Exception as exc:
                raise StoreError(f"list {full}: {exc}") from exc
            pages.append(page)
            if not page.get("IsTruncated"):
                return pages
            token = page.get("NextContinuationToken")

    def list_keys(self, prefix: str) -> list[str]:
        pages = self._list_pages(prefix, None)
        return sorted(self._relative(o["Key"]) for p in pages for o in p.get("Contents", []))

    def list_children(self, prefix: str) -> list[str]:
        if not prefix.endswith("/"):
            raise ValueError(f"list_children: prefix must end in '/': {prefix!r}")
        pages = self._list_pages(prefix, "/")
        full = self._full(prefix) + "/"
        names = (
            c["Prefix"][len(full) :].strip("/") for p in pages for c in p.get("CommonPrefixes", [])
        )
        return sorted(n for n in names if n)


# ---------------------------------------------------------------------------
# Configuration, the same way the artifacts plugin reads it
# ---------------------------------------------------------------------------

BACKEND_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/backend"
DESTINATION_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/destination"
ENDPOINT_URL_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/endpoint-url"
R2_ACCOUNT_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/account-id"
R2_ACCESS_KEY_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/access-key-id"
R2_SECRET_ACCESS_KEY_SSM_PARAM = "/services/buildkite/credentials/artifacts/r2/secret-access-key"
DEFAULT_SSM_REGION = "eu-west-1"

_PLACEHOLDER_PREFIXES = ("SET_ME", "REPLACE_", "TODO", "CHANGEME", "FIXME")

SsmGetter = Callable[[str], "str | None"]


@dataclass(frozen=True)
class StoreConfig:
    """Where the store is and how to authenticate. Credentials never appear in repr."""

    backend: str
    bucket: str
    prefix: str = ""
    endpoint_url: str | None = None
    access_key_id: str | None = field(default=None, repr=False)
    secret_access_key: str | None = field(default=None, repr=False)


def parse_destination(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/prefix`` (or a bare bucket name) into (bucket, prefix)."""
    if not uri.startswith("s3://"):
        return uri.strip("/"), ""
    rest = uri[len("s3://") :]
    bucket, _, prefix = rest.partition("/")
    return bucket, prefix.strip("/")


def _env_or_ssm(env: Mapping[str, str], ssm_get: SsmGetter, name: str, param: str) -> str | None:
    return env.get(name) or ssm_get(param)


def _required(value: str | None, label: str, env_name: str, param: str) -> str:
    if not value:
        raise StoreConfigError(f"{label} not configured: set {env_name} or SSM parameter {param}")
    if any(value.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        raise StoreConfigError(f"{label} holds a placeholder value (SSM {param})")
    return value


def load_store_config(env: Mapping[str, str], ssm_get: SsmGetter) -> StoreConfig:
    """Resolve the store configuration: ``ARTIFACTS_*`` variables, then SSM, then defaults.

    Same variables, parameters, defaults and precedence as the artifacts
    plugin's ``load_store_config``: backend ``s3`` or ``r2`` (default ``s3``);
    destination required; for R2 the account id, access key id and secret
    access key are required and the endpoint defaults to the account's R2 URL.
    For S3 the boto3 default credential chain is used (as ``resolve_object_auth``
    does). ``ssm_get(name)`` returns a parameter's decrypted value or None.
    """
    backend = (env.get("ARTIFACTS_BACKEND") or ssm_get(BACKEND_SSM_PARAM) or "s3").strip().lower()
    if backend not in ("s3", "r2"):
        raise StoreConfigError(f"unsupported artifact backend {backend!r} (expected s3 or r2)")
    destination = _required(
        _env_or_ssm(env, ssm_get, "ARTIFACTS_DESTINATION", DESTINATION_SSM_PARAM),
        "artifact destination",
        "ARTIFACTS_DESTINATION",
        DESTINATION_SSM_PARAM,
    )
    bucket, prefix = parse_destination(destination)
    endpoint_url = env.get("ARTIFACTS_ENDPOINT_URL")
    if backend == "s3":
        return StoreConfig(backend, bucket, prefix, endpoint_url)

    endpoint_url = endpoint_url or ssm_get(ENDPOINT_URL_SSM_PARAM)
    account = _required(
        _env_or_ssm(env, ssm_get, "ARTIFACTS_R2_ACCOUNT_ID", R2_ACCOUNT_ID_SSM_PARAM),
        "R2 account id",
        "ARTIFACTS_R2_ACCOUNT_ID",
        R2_ACCOUNT_ID_SSM_PARAM,
    )
    key_id = _required(
        _env_or_ssm(env, ssm_get, "ARTIFACTS_R2_ACCESS_KEY_ID", R2_ACCESS_KEY_ID_SSM_PARAM),
        "R2 access key id",
        "ARTIFACTS_R2_ACCESS_KEY_ID",
        R2_ACCESS_KEY_ID_SSM_PARAM,
    )
    secret = _required(
        _env_or_ssm(env, ssm_get, "ARTIFACTS_R2_SECRET_ACCESS_KEY", R2_SECRET_ACCESS_KEY_SSM_PARAM),
        "R2 secret access key",
        "ARTIFACTS_R2_SECRET_ACCESS_KEY",
        R2_SECRET_ACCESS_KEY_SSM_PARAM,
    )
    return StoreConfig(
        backend=backend,
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint_url or f"https://{account}.r2.cloudflarestorage.com",
        access_key_id=key_id,
        secret_access_key=secret,
    )


def ssm_getter(env: Mapping[str, str]) -> SsmGetter:
    """An ``SsmGetter`` on boto3, in ``AWS_DEFAULT_REGION`` (default eu-west-1).

    A missing parameter is None; any other SSM error raises ``StoreConfigError``
    so that a permission problem is not mistaken for "not configured".
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    client = boto3.session.Session().client(
        "ssm",
        config=Config(
            region_name=env.get("AWS_DEFAULT_REGION", DEFAULT_SSM_REGION),
            retries={"mode": "standard", "max_attempts": 5},
        ),
    )

    def get(name: str) -> str | None:
        try:
            return client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ParameterNotFound":
                return None
            raise StoreConfigError(f"failed to read SSM parameter {name}: {code}") from exc

    return get


def s3_client(cfg: StoreConfig) -> Any:
    """A boto3 S3 client for ``cfg``, with botocore's own retries switched off.

    R2 needs signature v4, path-style addressing and region "auto", as in the
    artifacts plugin.
    """
    import boto3
    from botocore.config import Config

    options: dict[str, Any] = {
        "read_timeout": 300,
        "connect_timeout": 60,
        "retries": {"mode": "standard", "max_attempts": 1},
    }
    kwargs: dict[str, Any] = {}
    if cfg.backend == "r2":
        options.update(signature_version="s3v4", s3={"addressing_style": "path"})
        kwargs["region_name"] = "auto"
    if cfg.endpoint_url:
        kwargs["endpoint_url"] = cfg.endpoint_url
    if cfg.access_key_id:
        kwargs["aws_access_key_id"] = cfg.access_key_id
        kwargs["aws_secret_access_key"] = cfg.secret_access_key
    return boto3.client("s3", config=Config(**options), **kwargs)


def open_store(env: Mapping[str, str]) -> S3Store:
    """The real store, configured from ``env`` and SSM like the artifacts plugin."""
    cfg = load_store_config(env, ssm_getter(env))
    return S3Store(s3_client(cfg), cfg.bucket, cfg.prefix)


class LazyStore:
    """A ``Store`` that opens the real one on first use.

    Lets a command that may not need the store (``plan --dry-run`` on a graph
    without opaque nodes) run without store credentials.
    """

    def __init__(self, opener: Callable[[], Store]) -> None:
        self._opener = opener
        self._store: Store | None = None

    def _s(self) -> Store:
        if self._store is None:
            self._store = self._opener()
        return self._store

    def get(self, key: str) -> StoredObject | None:
        return self._s().get(key)

    def put(
        self, key: str, body: bytes, *, if_none_match: bool = False, if_match: str | None = None
    ) -> bool:
        return self._s().put(key, body, if_none_match=if_none_match, if_match=if_match)

    def list_keys(self, prefix: str) -> list[str]:
        return self._s().list_keys(prefix)

    def list_children(self, prefix: str) -> list[str]:
        return self._s().list_children(prefix)
