"""Persist Actions SQLite snapshots in a separate, compare-and-swap Git ref."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import zlib


REF = "refs/heads/intelligence-state"
CHUNK = 8 * 1024 * 1024
DB_NAME = "intelligence.sqlite3"


def git(*args, data=None):
    return subprocess.check_output(["git", *args], input=data)


def git_oid(*args, data=None):
    return git(*args, data=data).decode("ascii").strip()


def cipher_for(env):
    if env.get("STATE_ENCRYPTION_KEY"):
        from cryptography.fernet import Fernet

        try:
            return Fernet(env["STATE_ENCRYPTION_KEY"].encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("STATE_ENCRYPTION_KEY must be a URL-safe base64 encoded 32-byte key") from exc
    if env.get("REPOSITORY_PRIVATE") == "true":
        return None
    raise ValueError("Public SQLite state requires STATE_ENCRYPTION_KEY")


def pack(data, cipher):
    return cipher.encrypt(data) if cipher else data


def unpack(data, cipher):
    if not cipher:
        return data
    from cryptography.fernet import InvalidToken

    try:
        return cipher.decrypt(data)
    except InvalidToken as exc:
        raise ValueError("Cannot authenticate state. Verify STATE_ENCRYPTION_KEY before running.") from exc


def verify(dbfile):
    with sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Stored SQLite state failed integrity checks")
        for table in ("reports", "sources", "slots", "spend"):
            db.execute(f"SELECT 1 FROM {table} LIMIT 0")
        if not db.execute("SELECT 1 FROM schema_versions WHERE version=1").fetchone():
            raise ValueError("Stored SQLite state has an unknown schema")


def remote_head():
    return git("ls-remote", "--refs", "origin", REF).decode().partition("\t")[0]


def read_manifest(head, cipher):
    header = git("show", f"{head}:manifest")
    encoding, separator, encoded = header.partition(b"\n")
    if not separator or encoding != (b"fernet-v1" if cipher else b"plain-v1"):
        raise ValueError("State encryption mode or format differs from current settings")
    return json.loads(unpack(encoded, cipher))


def restore(env, initialize=False):
    directory = Path(env["CLOUD_STATE_DIR"]).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    cipher = cipher_for(env)
    if not remote_head():
        if not initialize:
            raise RuntimeError(
                "State branch missing. Run workflow_dispatch with initialize_state on first setup."
            )
        from .storage import Database

        (directory / "head").write_text("")
        database = Database("sqlite:///" + str(directory / DB_NAME))
        database.close()
        checkpoint(env, directory / DB_NAME)
        return
    git("fetch", "--quiet", "--depth=1", "--no-tags", "origin", REF)
    head = git_oid("rev-parse", "--verify", "FETCH_HEAD^{commit}")
    manifest = read_manifest(head, cipher)
    size = manifest["size"]
    if not isinstance(size, int) or size < 1:
        raise ValueError("Invalid SQLite state size")
    dbfile = directory / DB_NAME
    digest = hashlib.sha256()
    with dbfile.open("xb") as db:
        for index, offset in enumerate(range(0, size, CHUNK)):
            payload = git("show", f"{head}:part-{index:08d}")
            decoder = zlib.decompressobj()
            block = decoder.decompress(unpack(payload, cipher), CHUNK + 1)
            if not decoder.eof or decoder.unused_data or len(block) != min(CHUNK, size - offset):
                raise ValueError("Incomplete SQLite state chunk")
            digest.update(block)
            db.write(block)
    if digest.hexdigest() != manifest["sha256"]:
        raise ValueError("SQLite state checksum mismatch")
    verify(dbfile)
    (directory / "head").write_text(head)


def checkpoint(env, dbfile):
    """Publish a consistent SQLite backup only if our restored remote head still matches."""
    directory = Path(env["CLOUD_STATE_DIR"]).resolve()
    if dbfile != directory / DB_NAME:
        raise ValueError("Checkpoint path does not match the restored SQLite database")
    head_file = directory / "head"
    pending = directory / "pending-push"
    retrying_push = pending.exists()
    if retrying_push:
        # A failed push can have updated the ref before the connection dropped.
        # Recover our own exact snapshot, never a concurrent writer's generation.
        candidate = pending.read_text().split()[0]
        remote = remote_head()
        if remote not in (candidate, head_file.read_text()):
            raise RuntimeError("The remote state was changed by another writer")
        head_file.write_text(remote)
        pending.unlink()
    previous = head_file.read_text()
    cipher = cipher_for(env)
    with tempfile.TemporaryDirectory(dir=directory) as folder:
        copy = Path(folder) / "snapshot"
        with sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True) as src, sqlite3.connect(copy) as dst:
            src.backup(dst)
        verify(copy)
        blobs = {}
        with copy.open("rb") as snapshot:
            digest = hashlib.file_digest(snapshot, "sha256").hexdigest()
            if previous and not retrying_push and read_manifest(previous, cipher)["sha256"] == digest:
                return
            snapshot.seek(0)
            for index, block in enumerate(iter(lambda: snapshot.read(CHUNK), b"")):
                payload = pack(zlib.compress(block, level=5), cipher)
                blobs[f"part-{index:08d}"] = git_oid("hash-object", "-w", "--stdin", data=payload)
        metadata = json.dumps({"size": copy.stat().st_size, "sha256": digest}).encode()
        header = (b"fernet-v1\n" if cipher else b"plain-v1\n") + pack(metadata, cipher)
        blobs["manifest"] = git_oid("hash-object", "-w", "--stdin", data=header)
        entries = "".join(f"100644 blob {oid}\t{name}\n" for name, oid in sorted(blobs.items()))
        tree = git_oid("mktree", data=entries.encode())
        # Intentionally parentless: the state branch tracks only its current snapshot.
        head = git_oid(
            "-c",
            "user.name=Investment Intelligence",
            "-c",
            "user.email=actions@users.noreply.github.com",
            "commit-tree",
            tree,
            "-m",
            "Investment Intelligence SQLite checkpoint",
        )
        pending.write_text(head)
        git("push", "--quiet", f"--force-with-lease={REF}:{previous}", "origin", f"{head}:{REF}")
        head_file.write_text(head)
        pending.unlink()


def before_external(db, env):
    if not db.pg and env.get("CLOUD_STATE_DIR"):
        checkpoint(env, db.path)


def main(argv):
    env = os.environ
    if env.get("DATABASE_URL"):
        if not env["DATABASE_URL"].startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
            raise ValueError("Set DATABASE_URL to PostgreSQL, or omit it for managed SQLite")
        return
    if argv == ["restore"]:
        if env.get("GITHUB_REF_NAME") == "intelligence-state":
            raise ValueError("The application and state branches must differ")
        restore(env, initialize=env.get("INITIALIZE_STATE") == "true")
    elif argv == ["save"]:
        directory = Path(env["CLOUD_STATE_DIR"]).resolve()
        checkpoint(env, directory / DB_NAME)
    else:
        raise ValueError("Expected restore or save")


if __name__ == "__main__":
    main(sys.argv[1:])
