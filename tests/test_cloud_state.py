"""Use a local Git remote to exercise cloud failure and recovery boundaries."""

import subprocess

import pytest

from investment_intelligence import cloud_state as state
from investment_intelligence.configuration import Settings
from investment_intelligence.delivery import deliver
from investment_intelligence.storage import Database, execute


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    source = tmp_path / "checkout"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    monkeypatch.chdir(source)
    state.git("remote", "add", "origin", str(remote))
    return tmp_path


def environment(folder, **kwargs):
    return {
        "GITHUB_ACTIONS": "true",
        "REPOSITORY_PRIVATE": "true",
        "CLOUD_STATE_DIR": str(folder),
        "OUTPUT_DIR": str(folder / "output"),
        "SMTP_HOST": "smtp.example.test",
        "SMTP_TO": "reader@example.test",
        "SMTP_FROM": "system@example.test",
        **kwargs,
    }


def seed(db):
    with db.write() as conn:
        execute(
            conn,
            """INSERT INTO reports VALUES
            ('alert-id','alert.md','# Private research sample','2026-11-11','ready',NULL)""",
        )


def test_native_cloud_preserves_claim_before_smtp_even_when_runner_disappears(repo):
    env = environment(repo / "first")
    with pytest.raises(RuntimeError, match="State branch missing"):
        state.restore(environment(repo / "missing"))
    state.restore(env, initialize=True)
    settings = Settings(env)
    db = Database(settings.url)
    seed(db)

    class SimulatedRunnerLoss(BaseException):
        pass

    def accept_then_crash(*args):
        raise SimulatedRunnerLoss

    with pytest.raises(SimulatedRunnerLoss):
        deliver(db, settings, "alert-id", send=accept_then_crash)
    db.close()
    second = environment(repo / "second")
    state.restore(second)
    restored = Database(Settings(second).url)
    assert (
        deliver(restored, Settings(second), "alert-id", send=lambda *_: pytest.fail("duplicate"))[1]
        == "sending"
    )
    restored.close()


def test_cloud_state_encryption_conflict_and_integrity(repo, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(state, "CHUNK", 16384)
    key = Fernet.generate_key().decode()

    def encrypted(name, key=key):
        return environment(repo / name, REPOSITORY_PRIVATE="false", STATE_ENCRYPTION_KEY=key)

    first = encrypted("first")
    state.restore(first, initialize=True)
    db = Database(Settings(first).url)
    seed(db)
    state.checkpoint(first, db.path)
    assert state.git("show", (repo / "first/head").read_text() + ":manifest").startswith(b"fernet-v1\n")
    with pytest.raises(ValueError, match="authenticate"):
        state.restore(encrypted("wrongkey", Fernet.generate_key().decode()), initialize=True)
    stale = encrypted("stale")
    state.restore(stale)
    with db.write() as conn:
        execute(conn, "UPDATE reports SET mail_state='sent' WHERE report_id='alert-id'")
    state.checkpoint(first, db.path)
    head = (repo / "first/head").read_text()
    assert len(state.git("rev-list", "--parents", "-n1", head).split()) == 1
    competing = Database(Settings(stale).url)
    with competing.write() as conn:
        execute(conn, "UPDATE reports SET mail_state='uncertain'")
    with pytest.raises(subprocess.CalledProcessError):
        state.checkpoint(stale, competing.path)
    final = encrypted("final")
    state.restore(final)
    restored = Database(Settings(final).url)
    assert restored.all("SELECT mail_state FROM reports") == [{"mail_state": "sent"}]
    restored.close()
    competing.close()
    db.close()


def test_public_state_always_requires_encryption():
    with pytest.raises(ValueError, match="STATE_ENCRYPTION_KEY"):
        state.cipher_for({"REPOSITORY_PRIVATE": "false"})
    assert state.cipher_for({"REPOSITORY_PRIVATE": "true"}) is None


@pytest.mark.parametrize("lost_ack", [False, True])
def test_checkpoint_failure_keeps_unsent_mail_retryable_across_exit_checkpoint(repo, monkeypatch, lost_ack):
    env = environment(repo / "first")
    state.restore(env, initialize=True)
    settings = Settings(env)
    db = Database(settings.url)
    seed(db)

    original_git = state.git

    def fail_push(*args, **kwargs):
        if args[0] != "push":
            return original_git(*args, **kwargs)
        if lost_ack:
            original_git(*args, **kwargs)
        raise RuntimeError("checkpoint failed")

    with monkeypatch.context() as patch:
        patch.setattr(state, "git", fail_push)
        with pytest.raises(RuntimeError, match="checkpoint failed"):
            deliver(db, settings, "alert-id", send=lambda *_: pytest.fail("sent before checkpoint"))
    # Simulate the workflow EXIT trap, then the next runner.
    state.checkpoint(env, db.path)
    db.close()
    recovery = environment(repo / "recovery")
    state.restore(recovery)
    settings = Settings(recovery)
    restored = Database(settings.url)
    assert restored.all("SELECT mail_state FROM reports") == [{"mail_state": "ready"}]
    calls = []
    assert deliver(restored, settings, "alert-id", send=lambda *_: calls.append(True))[1] == "sent"
    assert calls == [True]
    restored.close()
