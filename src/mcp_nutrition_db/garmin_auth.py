"""Interactive login and private, refreshable runtime sessions. Never logs secrets."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import importlib
import json
import logging
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .serialization import canonical_json, new_id, parse_timestamp, utc_now

CLIENT_VERSION = "0.3.16"


def private_write(path: Path, payload: str, *, replace: bool = True) -> None:
    """Create owner-only files before writing any bytes; publish atomically."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{new_id()}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def session_lock(state: Path) -> Iterator[None]:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.is_symlink() or state.stat().st_uid != os.getuid():
        raise ValueError("runtime state must be an owned directory, not a symlink")
    state.chmod(0o700)
    fd = os.open(state / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another Garmin login or sync is running") from None
        yield


def client(**kwargs: Any) -> Any:
    from importlib.metadata import version

    if version("garminconnect") != CLIENT_VERSION:
        raise ValueError("unsupported Garmin client version")
    # Third-party authentication diagnostics can include private response bodies.
    logging.getLogger("garminconnect").disabled = True
    logging.getLogger("garminconnect.client").disabled = True
    return importlib.import_module("garminconnect").Garmin(retry_attempts=2, **kwargs)


def account_id(api: Any) -> str:
    value = api.profile_id
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("Garmin did not return a permanent account ID")
    return str(value)


def login(output: Path) -> None:
    """Export a fresh session only after a second client resumes it successfully."""
    if output.exists():
        raise ValueError("output already exists; choose a new private output filename")
    if not os.isatty(0):
        raise ValueError("login requires a private interactive terminal")
    api = client(
        email=getpass.getpass("Garmin email (hidden): "),
        password=getpass.getpass("Garmin password: "),
        prompt_mfa=lambda: getpass.getpass("Garmin MFA code: "),
    )
    inherited_tokens = os.environ.pop("GARMINTOKENS", None)
    try:
        api.login()
    finally:
        if inherited_tokens is not None:
            os.environ["GARMINTOKENS"] = inherited_tokens
    identity = account_id(api)
    resumed = client()
    resumed.login(api.client.dumps())
    if account_id(resumed) != identity:
        raise ValueError("saved-session account mismatch")
    bundle = {
        "format": 1,
        "client_version": CLIENT_VERSION,
        "account_id": identity,
        "session_id": new_id(),
        "tokens": json.loads(resumed.client.dumps()),
        "login_validated": True,
        "session_restart_validated": True,
    }
    private_write(output, canonical_json(bundle), replace=False)


def seed_session(state: Path, credential: Path | None) -> dict[str, Any]:
    """Install a new seed generation once; never replace refreshed state on restart."""
    metadata_path = state / "account.json"
    metadata: dict[str, Any] = (
        json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    )
    if credential is not None:
        bundle = json.loads(credential.read_text())
        if bundle.get("format") != 1 or bundle.get("client_version") != CLIENT_VERSION:
            raise ValueError("unsupported session bundle")
        if not bundle.get("account_id") or not bundle.get("session_id"):
            raise ValueError("incomplete session bundle")
        if metadata and metadata["account_id"] != bundle["account_id"]:
            raise ValueError(
                "runtime account differs from credential; use a separate state directory"
            )
        if metadata.get("session_id") != bundle["session_id"]:
            # Metadata is written last: interruption causes a safe reseed on retry.
            private_write(state / "tokens.json", canonical_json(bundle["tokens"]))
            metadata = {k: v for k, v in bundle.items() if k != "tokens"}
            metadata["auth_state"] = "ready"
            private_write(metadata_path, canonical_json(metadata))
    if not metadata or not (state / "tokens.json").exists():
        raise ValueError("no session: run the Garmin login app and configure credentialsFile")
    return metadata


def resume(state: Path, metadata: dict[str, Any]) -> Any:
    if metadata.get("auth_state") == "reauth_required":
        raise ValueError("reauth_required: provide a new login session")
    if metadata.get("next_attempt_at") and utc_now() < parse_timestamp(metadata["next_attempt_at"]):
        raise ValueError("rate limit backoff active")
    path = state / "tokens.json"
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("token file must be private and not a symlink")
    try:
        api = client()
        # systemd DynamicUser state has a trusted symlink in its ancestry.
        # Read the checked private file ourselves; the client rejects such paths.
        api.login(path.read_text())
        if account_id(api) != metadata["account_id"]:
            raise ValueError("account mismatch")
        private_write(path, api.client.dumps())
        return api
    except Exception as error:
        if "TooManyRequests" in type(error).__name__:
            from datetime import timedelta

            from .serialization import timestamp

            metadata["next_attempt_at"] = timestamp(utc_now() + timedelta(hours=1))
            private_write(state / "account.json", canonical_json(metadata))
        if "Authentication" in type(error).__name__ or str(error) == "account mismatch":
            metadata["auth_state"] = "reauth_required"
            private_write(state / "account.json", canonical_json(metadata))
        raise ValueError("Garmin session unavailable; inspect sync status or renew login") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a private Garmin session for sops")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        login(args.output.expanduser())
    except Exception:
        print(
            "Login failed; no session exported. Check credentials/MFA and retry in this terminal."
        )
        return 1
    print(f"Session saved privately to {args.output}.")
    print("Encrypt this file with sops; do not commit plaintext.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
