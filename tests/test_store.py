from __future__ import annotations

import io

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from snapdep.fakes import MemoryStore
from snapdep.store import (
    ABSENT,
    FATAL,
    PRECONDITION,
    RETRY,
    RetryPolicy,
    S3Store,
    StoreConfigError,
    StoreError,
    classify,
    key_join,
    load_store_config,
    parse_destination,
)

# ---------------------------------------------------------------------------
# MemoryStore: the conditional semantics the records rely on
# ---------------------------------------------------------------------------


def test_create_once():
    s = MemoryStore()
    assert s.put("k", b"1", if_none_match=True)
    assert not s.put("k", b"2", if_none_match=True)
    assert s.get("k").body == b"1"


def test_compare_and_swap():
    s = MemoryStore()
    s.put("k", b"1")
    etag = s.get("k").etag
    assert s.put("k", b"2", if_match=etag)
    assert not s.put("k", b"3", if_match=etag), "the etag moved with the write"
    assert not s.put("missing", b"x", if_match=etag)
    assert s.get("k").body == b"2"


def test_same_body_gets_a_new_etag():
    s = MemoryStore()
    s.put("k", b"1")
    first = s.get("k").etag
    s.put("k", b"1")
    assert s.get("k").etag != first


def test_plain_put_overwrites_and_both_conditions_are_rejected():
    s = MemoryStore({"k": b"1"})
    assert s.put("k", b"2")
    assert s.get("k").body == b"2"
    with pytest.raises(ValueError):
        s.put("k", b"3", if_none_match=True, if_match='"x"')


def test_listing():
    s = MemoryStore({"a/x/1": b"", "a/x/2": b"", "a/y/LATEST": b"", "a/z": b"", "b/q/1": b""})
    assert s.list_keys("a/x/") == ["a/x/1", "a/x/2"]
    assert s.list_children("a/") == ["x", "y"]
    assert s.get("nope") is None


def test_unreadable_key_raises():
    with pytest.raises(StoreError):
        MemoryStore(unreadable=["k"]).get("k")


# ---------------------------------------------------------------------------
# Error policy
# ---------------------------------------------------------------------------


def client_error(status: int, code: str, op: str = "PutObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


@pytest.mark.parametrize(
    "exc, kind",
    [
        (client_error(412, "PreconditionFailed"), PRECONDITION),
        (client_error(404, "NoSuchKey", "GetObject"), ABSENT),
        (client_error(429, "TooManyRequests"), RETRY),
        (client_error(503, "SlowDown"), RETRY),
        (client_error(500, "InternalError"), RETRY),
        (client_error(403, "AccessDenied"), FATAL),
        (EndpointConnectionError(endpoint_url="https://x"), RETRY),
        (ValueError("x"), FATAL),
    ],
)
def test_classify(exc, kind):
    assert classify(exc) == kind


class StubS3:
    """A boto3-like client that replays scripted outcomes per operation."""

    def __init__(self, **scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def _next(self, op, kwargs):
        self.calls.append((op, kwargs))
        outcome = self.scripts[op].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get_object(self, **kw):
        return self._next("get_object", kw)

    def put_object(self, **kw):
        return self._next("put_object", kw)

    def list_objects_v2(self, **kw):
        return self._next("list_objects_v2", kw)


def store(client, attempts=4):
    return S3Store(
        client,
        "bucket",
        "pfx",
        policy=RetryPolicy(attempts=attempts, base=0, cap=0),
        sleep=lambda _: None,
        log=lambda _: None,
    )


def test_s3_get_found_absent_and_unanswerable():
    found = {"Body": io.BytesIO(b"{}"), "ETag": '"e"'}
    c = StubS3(get_object=[client_error(503, "SlowDown", "GetObject"), found])
    o = store(c).get("_snapdep/v1/x")
    assert (o.body, o.etag) == (b"{}", '"e"')
    assert c.calls[0][1] == {"Bucket": "bucket", "Key": "pfx/_snapdep/v1/x"}

    c = StubS3(get_object=[client_error(404, "NoSuchKey", "GetObject")])
    assert store(c).get("x") is None

    c = StubS3(get_object=[client_error(500, "InternalError", "GetObject")] * 4)
    with pytest.raises(StoreError, match="after 4 attempts"):
        store(c).get("x")

    c = StubS3(get_object=[client_error(403, "AccessDenied", "GetObject")])
    with pytest.raises(StoreError, match="AccessDenied"):
        store(c).get("x")


def test_s3_create_race_loser_429_then_412_is_an_answer():
    c = StubS3(put_object=[client_error(429, "TooManyRequests"), client_error(412, "Precondition")])
    assert store(c).put("k", b"1", if_none_match=True) is False
    assert c.calls[0][1]["IfNoneMatch"] == "*"
    assert "IfMatch" not in c.calls[0][1]


def test_s3_put_conditions_and_errors():
    c = StubS3(put_object=[{}])
    assert store(c).put("k", b"1", if_match='"e"') is True
    assert c.calls[0][1]["IfMatch"] == '"e"'

    c = StubS3(put_object=[client_error(412, "PreconditionFailed")])
    with pytest.raises(StoreError):
        store(c).put("k", b"1")

    c = StubS3(put_object=[client_error(403, "AccessDenied")])
    with pytest.raises(StoreError):
        store(c).put("k", b"1", if_none_match=True)


def test_s3_list_pages_and_children():
    c = StubS3(
        list_objects_v2=[
            {
                "IsTruncated": True,
                "NextContinuationToken": "t",
                "Contents": [{"Key": "pfx/a/2"}],
            },
            {"IsTruncated": False, "Contents": [{"Key": "pfx/a/1"}]},
        ]
    )
    assert store(c).list_keys("a/") == ["a/1", "a/2"]
    assert c.calls[1][1]["ContinuationToken"] == "t"
    assert c.calls[0][1]["Prefix"] == "pfx/a/"

    c = StubS3(
        list_objects_v2=[
            {
                "IsTruncated": False,
                "CommonPrefixes": [{"Prefix": "pfx/p/v/x/"}, {"Prefix": "pfx/p/v/y/"}],
            }
        ]
    )
    assert store(c).list_children("p/v/") == ["x", "y"]
    assert c.calls[0][1]["Delimiter"] == "/"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_key_join_and_destination():
    assert key_join("", "/a/", "b", "") == "a/b"
    assert parse_destination("s3://bucket") == ("bucket", "")
    assert parse_destination("s3://bucket/some/prefix/") == ("bucket", "some/prefix")
    assert parse_destination("bucket") == ("bucket", "")


def no_ssm(name):
    return None


def test_config_s3_from_env():
    cfg = load_store_config({"ARTIFACTS_DESTINATION": "s3://b/p"}, no_ssm)
    assert (cfg.backend, cfg.bucket, cfg.prefix, cfg.endpoint_url) == ("s3", "b", "p", None)
    assert cfg.access_key_id is None


def test_config_r2_from_ssm_env_wins_and_secret_not_in_repr():
    ssm = {
        "/services/buildkite/config/artifacts/object-store/backend": "r2",
        "/services/buildkite/config/artifacts/object-store/destination": "s3://ssm-bucket",
        "/services/buildkite/config/artifacts/object-store/r2/account-id": "acct",
        "/services/buildkite/config/artifacts/object-store/r2/access-key-id": "kid",
        "/services/buildkite/credentials/artifacts/r2/secret-access-key": "s3cr3t",
    }
    cfg = load_store_config({"ARTIFACTS_DESTINATION": "s3://env-bucket"}, ssm.get)
    assert cfg.backend == "r2"
    assert cfg.bucket == "env-bucket"
    assert cfg.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert (cfg.access_key_id, cfg.secret_access_key) == ("kid", "s3cr3t")
    assert "s3cr3t" not in repr(cfg) and "kid" not in repr(cfg)


def test_config_errors():
    with pytest.raises(StoreConfigError, match="ARTIFACTS_DESTINATION"):
        load_store_config({}, no_ssm)
    with pytest.raises(StoreConfigError, match="unsupported"):
        load_store_config({"ARTIFACTS_BACKEND": "gcs"}, no_ssm)
    env = {"ARTIFACTS_BACKEND": "r2", "ARTIFACTS_DESTINATION": "b"}
    with pytest.raises(StoreConfigError, match="R2 account id"):
        load_store_config(env, no_ssm)
    env.update(ARTIFACTS_R2_ACCOUNT_ID="SET_ME")
    with pytest.raises(StoreConfigError, match="placeholder"):
        load_store_config(env, no_ssm)
