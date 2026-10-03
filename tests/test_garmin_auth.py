import json
import os
from datetime import UTC

import pytest

from mcp_nutrition_db import garmin_auth


def bundle(session="first", account="123", token="synthetic-seed"):
    return {
        "format": 1,
        "client_version": garmin_auth.CLIENT_VERSION,
        "account_id": account,
        "session_id": session,
        "tokens": {"di_token": token},
        "login_validated": True,
        "session_restart_validated": True,
    }


def test_seed_preserves_rotation_and_new_generation(tmp_path):
    state = tmp_path / "state"
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps(bundle()))
    with garmin_auth.session_lock(state):
        garmin_auth.seed_session(state, secret)
        token_file = state / "tokens.json"
        token_file.write_text('{"di_token":"synthetic-refreshed"}')
        garmin_auth.seed_session(state, secret)
        assert json.loads(token_file.read_text())["di_token"] == "synthetic-refreshed"
        secret.write_text(json.dumps(bundle("second")))
        garmin_auth.seed_session(state, secret)
        assert json.loads(token_file.read_text())["di_token"] == "synthetic-seed"
        assert token_file.stat().st_mode & 0o777 == 0o600
        secret.write_text(json.dumps(bundle("third", account="456")))
        with pytest.raises(ValueError, match="account"):
            garmin_auth.seed_session(state, secret)


def test_concurrent_sync_and_login_are_serialized(tmp_path):
    with (
        garmin_auth.session_lock(tmp_path / "state"),
        pytest.raises(ValueError, match="another Garmin"),
        garmin_auth.session_lock(tmp_path / "state"),
    ):
        pytest.fail("lock must prevent simultaneous refresh")


def test_private_write_refuses_overwrite_and_symlink(tmp_path):
    output = tmp_path / "bundle.json"
    garmin_auth.private_write(output, "first", replace=False)
    with pytest.raises(FileExistsError):
        garmin_auth.private_write(output, "second", replace=False)
    assert output.read_text() == "first"
    (tmp_path / "link").symlink_to(output)
    with pytest.raises(FileExistsError):
        garmin_auth.private_write(tmp_path / "link", "second", replace=False)
    assert not list(tmp_path.glob(".bundle.json.*"))


def test_login_resume_mfa_and_no_secret_output(tmp_path, monkeypatch, capsys):
    class Tokens:
        def dumps(self):
            return '{"di_token":"synthetic","di_refresh_token":"synthetic-refresh"}'

    class API:
        profile_id = 123
        client = Tokens()

        def login(self, tokens=None):
            if tokens:
                assert "synthetic-refresh" in tokens

    prompts = []

    def factory(**kwargs):
        if "prompt_mfa" in kwargs:
            kwargs["prompt_mfa"]()
        return API()

    monkeypatch.setattr(garmin_auth, "client", factory)
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    monkeypatch.setattr(
        garmin_auth.getpass, "getpass", lambda prompt: prompts.append(prompt) or "private"
    )
    output = tmp_path / "session.json"
    garmin_auth.login(output)
    assert len(prompts) == 3
    assert json.loads(output.read_text())["account_id"] == "123"
    assert "synthetic" not in capsys.readouterr().out


def test_auth_retry_backoff_and_cap(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    state = tmp_path / "state"
    state.mkdir()
    garmin_auth.private_write(state / "tokens.json", "{}")
    now = datetime(2026, 10, 4, tzinfo=UTC)
    monkeypatch.setattr(garmin_auth, "utc_now", lambda: now)
    metadata = {"account_id": "123", "auth_state": "reauth_required"}

    class GarminConnectAuthenticationError(Exception):
        pass

    class API:
        @property
        def client(self):
            return self

        def login(self, path):
            raise GarminConnectAuthenticationError("private provider response")

    monkeypatch.setattr(garmin_auth, "client", API)
    for minutes in [30, 60, 120, 240, 360, 360]:
        with pytest.raises(ValueError, match="session unavailable"):
            garmin_auth.resume(state, metadata)
        assert garmin_auth.parse_timestamp(metadata["next_attempt_at"]) == (
            now + timedelta(minutes=minutes)
        )
        with pytest.raises(ValueError, match="backoff"):
            garmin_auth.resume(state, metadata)
        now += timedelta(minutes=minutes)
    assert metadata["auth_state"] == "reauth_required"
    assert "private provider response" not in (state / "account.json").read_text()


def test_resume_through_systemd_state_parent(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    public = tmp_path / "service"
    public.symlink_to(private, target_is_directory=True)
    state = public / "garmin"
    state.mkdir(mode=0o700)
    token = state / "tokens.json"
    garmin_auth.private_write(token, '{"di_token":"synthetic-old"}')

    class API:
        profile_id = 123

        def login(self, value):
            assert json.loads(__import__("pathlib").Path(value).read_text()) == {
                "di_token": "synthetic-old"
            }

        @property
        def client(self):
            return self

        def dumps(self):
            return '{"di_token":"synthetic-rotated"}'

    monkeypatch.setattr(garmin_auth, "client", API)
    with garmin_auth.session_lock(state):
        garmin_auth.resume(state, {"account_id": "123"})
    assert json.loads(token.read_text()) == {"di_token": "synthetic-rotated"}
    assert token.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("fail_after_refresh", [False, True])
def test_sync_persists_tokens_refreshed_during_fetch(tmp_path, monkeypatch, fail_after_refresh):
    from types import SimpleNamespace

    from mcp_nutrition_db import garmin_cli

    class API:
        token = "before-fetch"

        @property
        def client(self):
            return self

        def dumps(self):
            return json.dumps({"di_token": self.token})

    api = API()
    secret = tmp_path / "seed.json"
    secret.write_text(json.dumps(bundle()))
    state = tmp_path / "runtime"
    monkeypatch.setattr(garmin_cli, "resume", lambda state, metadata: api)

    def fetch(self, **kwargs):
        api.token = "after-refresh"
        if fail_after_refresh:
            raise ValueError("synthetic fetch failure")
        return {}

    monkeypatch.setattr(garmin_cli.GarminImporter, "sync", fetch)
    args = SimpleNamespace(
        garmin_command="sync",
        database=str(tmp_path / "db.sqlite3"),
        state_directory=state,
        credentials_file=secret,
        start=None,
        end=None,
        dry_run=False,
        weekly=False,
        output=None,
    )
    if fail_after_refresh:
        with pytest.raises(ValueError, match="synthetic fetch failure"):
            garmin_cli.run(args)
    else:
        assert garmin_cli.run(args) == 0
    assert json.loads((state / "tokens.json").read_text())["di_token"] == "after-refresh"


def test_rotation_saved_even_when_resume_fails(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    garmin_auth.private_write(state / "tokens.json", '{"di_token":"old"}')

    class API:
        @property
        def client(self):
            return self

        def dumps(self):
            return '{"di_token":"rotated","di_refresh_token":"rotated-refresh"}'

        def login(self, path):
            self.dump(path)
            raise ValueError("profile request failed after rotation")

    monkeypatch.setattr(garmin_auth, "client", API)
    with pytest.raises(ValueError, match="session unavailable"):
        garmin_auth.resume(state, {"account_id": "123"})
    assert json.loads((state / "tokens.json").read_text())["di_refresh_token"] == (
        "rotated-refresh"
    )


def test_library_refresh_persists_before_failed_profile_request(tmp_path, monkeypatch):
    from garminconnect import Garmin

    state = tmp_path / "state"
    state.mkdir()
    garmin_auth.private_write(
        state / "tokens.json",
        '{"di_token":"old","di_refresh_token":"old-refresh","di_client_id":"synthetic"}',
    )
    api = Garmin()

    def rotate():
        api.client.di_token = "new"
        api.client.di_refresh_token = "new-refresh"

    def login(path):
        api.client.load(path)
        api.client._refresh_session()
        raise ValueError("profile fetch failed")

    monkeypatch.setattr(api.client, "_refresh_di_token", rotate)
    monkeypatch.setattr(api, "login", login)
    monkeypatch.setattr(garmin_auth, "client", lambda: api)
    with pytest.raises(ValueError, match="session unavailable"):
        garmin_auth.resume(state, {"account_id": "123"})
    assert json.loads((state / "tokens.json").read_text())["di_refresh_token"] == "new-refresh"


def test_saved_session_recovers_from_reauth_and_account_mismatch_stays_blocked(
    tmp_path, monkeypatch
):
    state = tmp_path / "state"
    state.mkdir()
    garmin_auth.private_write(state / "tokens.json", '{"di_token":"old"}')

    class API:
        profile_id = 123

        @property
        def client(self):
            return self

        def dumps(self):
            return '{"di_token":"new"}'

        def login(self, path):
            pass

    monkeypatch.setattr(garmin_auth, "client", API)
    metadata = {"account_id": "123", "auth_state": "reauth_required"}
    garmin_auth.resume(state, metadata)
    assert metadata["auth_state"] == "ready"
    metadata["account_id"] = "456"
    with pytest.raises(ValueError, match="session unavailable"):
        garmin_auth.resume(state, metadata)
    assert metadata["account_mismatch"] is True
    monkeypatch.setattr(garmin_auth, "client", lambda: pytest.fail("wrong account"))
    with pytest.raises(ValueError, match="account mismatch"):
        garmin_auth.resume(state, metadata)
