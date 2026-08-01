"""In-game save sync: the NAND save archive RomM pulls and pushes back.

Eden has no emulator save states, so this path is the whole of its save story.
The guards that matter are the ones that decide whether a restore overwrites a
file: a mtime read in the wrong timezone, or a file zipped mid-write, silently
loses a user's progress.
"""

import calendar
import io
import os
import time
import zipfile
from pathlib import Path

import pytest

import broker
from conftest import request

SAVE_SUB = "nand/user/save"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """An Eden data dir with the save subtree in place."""
    root = tmp_path / "eden-data"
    (root / SAVE_SUB).mkdir(parents=True)
    monkeypatch.setattr(broker, "_save_data_root", lambda: root)
    # chown is a no-op for an unprivileged test run.
    monkeypatch.setattr(broker.os, "chown", lambda *a, **k: None)
    return root


def _save(root, rel, content=b"progress", mtime=None):
    p = root / SAVE_SUB / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _members(archive):
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        return sorted(i.filename for i in zf.infolist())


# ── Building the archive ──────────────────────────────────────────────────────


def test_archive_carries_files_modified_since_the_baseline(data_root):
    _save(data_root, "0100/game.dat", mtime=time.time())
    archive, unstable = broker._build_save_archive(time.time() - 60)
    assert unstable == 0
    assert _members(archive) == [f"{SAVE_SUB}/0100/game.dat"]


def test_archive_skips_files_older_than_the_baseline(data_root):
    _save(data_root, "0100/old.dat", mtime=time.time() - 3600)
    with pytest.raises(broker.NoSaveArchive) as exc:
        broker._build_save_archive(time.time() - 60)
    assert exc.value.status == 404


def test_archive_excludes_dot_prefixed_staging_files(data_root):
    """A concurrent restore writes `.name.tmp` beside its target; a pull that
    swept one in would ship a half-written file as if it were a save."""
    now = time.time()
    _save(data_root, "0100/game.dat", mtime=now)
    _save(data_root, "0100/.game.dat.tmp", content=b"half", mtime=now)
    archive, _ = broker._build_save_archive(now - 60)
    assert _members(archive) == [f"{SAVE_SUB}/0100/game.dat"]


def test_archive_stamps_member_mtimes_in_utc(data_root):
    """Written with gmtime and read back with timegm, so a GET and a PUT in
    containers with different TZ agree on when each save was written."""
    mtime = time.time()
    _save(data_root, "0100/game.dat", mtime=mtime)
    archive, _ = broker._build_save_archive(mtime - 60)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        stamped = calendar.timegm(zf.infolist()[0].date_time)
    # DOS timestamps round to 2 s, so allow that much slack.
    assert abs(stamped - mtime) <= 2


def test_archive_reports_oversize_rather_than_looking_empty(data_root, monkeypatch):
    """An operator must be able to tell 'nothing to sync' from 'sync refused'."""
    monkeypatch.setattr(broker, "SAVE_FILE_MAX_BYTES", 8)
    _save(data_root, "0100/big.dat", content=b"x" * 64, mtime=time.time())
    with pytest.raises(broker.NoSaveArchive) as exc:
        broker._build_save_archive(time.time() - 60)
    # 413, not the 404 of an empty sync — "refused" must not read as "nothing".
    assert exc.value.status == 413
    assert "size limit" in exc.value.message


def test_archive_refuses_when_every_changed_file_is_mid_write(data_root, monkeypatch):
    """Serving the empty archive instead would be recorded as a clean sync, and
    the save that was mid-write would never be pulled again."""
    _save(data_root, "0100/game.dat", mtime=time.time())
    monkeypatch.setattr(broker, "_read_file_stable", lambda p, **k: None)
    with pytest.raises(broker.NoSaveArchive) as exc:
        broker._build_save_archive(time.time() - 60)
    assert exc.value.status == 503


# ── Torn-read protection ──────────────────────────────────────────────────────


def test_read_file_stable_returns_content_for_a_settled_file(tmp_path):
    p = tmp_path / "save.dat"
    p.write_bytes(b"progress")
    result = broker._read_file_stable(p)
    assert result is not None and result[0] == b"progress"


def test_read_file_stable_gives_up_on_a_file_that_keeps_growing(tmp_path, monkeypatch):
    p = tmp_path / "save.dat"
    p.write_bytes(b"a")
    real_stat = Path.stat
    calls = {"n": 0}

    def growing_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self == p:
            calls["n"] += 1
            # Report a different size on every call, so before != after always.
            return os.stat_result(
                tuple(st)[:6] + (calls["n"],) + tuple(st)[7:]
            )
        return st

    monkeypatch.setattr(Path, "stat", growing_stat)
    monkeypatch.setattr(broker.time, "sleep", lambda s: None)
    assert broker._read_file_stable(p, retries=4) is None
    # Two stats per attempt — proves it retried rather than bailing on an error.
    assert calls["n"] == 8


# ── Restoring the archive ─────────────────────────────────────────────────────


def _archive(entries):
    """entries: {member: (content, mtime)} — stamped UTC like the real builder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, (content, mtime) in entries.items():
            zf.writestr(
                zipfile.ZipInfo(name, date_time=time.gmtime(mtime)[:6]), content
            )
    return buf.getvalue()


def test_restore_writes_members_into_the_save_subtree(data_root):
    now = time.time()
    content = _archive({f"{SAVE_SUB}/0100/game.dat": (b"restored", now)})
    written, skipped, failed = broker._extract_save_archive(content)
    assert (written, skipped, failed) == (1, 0, 0)
    assert (data_root / SAVE_SUB / "0100/game.dat").read_bytes() == b"restored"


def test_restore_round_trips_a_built_archive(data_root):
    """The two halves must agree: build, wipe, restore, same bytes and mtime."""
    mtime = time.time() - 300
    target = _save(data_root, "0100/game.dat", content=b"progress", mtime=mtime)
    archive, _ = broker._build_save_archive(mtime - 60)
    target.unlink()
    written, _, failed = broker._extract_save_archive(archive)
    assert (written, failed) == (1, 0)
    assert target.read_bytes() == b"progress"
    assert abs(target.stat().st_mtime - mtime) <= 2


def test_restore_does_not_roll_back_a_newer_local_save(data_root):
    """The whole point of the mtime guard: a stale pull must never clobber
    progress the user made after the archive was taken."""
    now = time.time()
    _save(data_root, "0100/game.dat", content=b"newer", mtime=now)
    content = _archive({f"{SAVE_SUB}/0100/game.dat": (b"older", now - 3600)})
    written, skipped, failed = broker._extract_save_archive(content)
    assert (written, skipped, failed) == (0, 1, 0)
    assert (data_root / SAVE_SUB / "0100/game.dat").read_bytes() == b"newer"


def test_restore_reports_a_failed_member_without_abandoning_the_rest(
    data_root, monkeypatch
):
    now = time.time()
    content = _archive({
        f"{SAVE_SUB}/0100/a.dat": (b"a", now),
        f"{SAVE_SUB}/0100/b.dat": (b"b", now),
    })
    real_write = Path.write_bytes

    def flaky_write(self, data):
        if self.name.startswith(".a.dat"):
            raise OSError("disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", flaky_write)
    written, skipped, failed = broker._extract_save_archive(content)
    assert (written, failed) == (1, 1)
    # The member that could be written still landed.
    assert (data_root / SAVE_SUB / "0100/b.dat").read_bytes() == b"b"


def test_restore_leaves_no_staging_file_when_a_member_fails(data_root, monkeypatch):
    """The staging file is dot-prefixed, and _iter_save_files hides dot-prefixed
    names from every later pull — so a temp left behind here is invisible litter
    that nothing else will ever sweep."""
    now = time.time()
    content = _archive({f"{SAVE_SUB}/0100/game.dat": (b"restored", now)})

    def failing_replace(src, dst):
        raise OSError("disk full")

    # Fails after the temp exists, unlike a write_bytes failure.
    monkeypatch.setattr(broker.os, "replace", failing_replace)
    written, _skipped, failed = broker._extract_save_archive(content)
    assert (written, failed) == (0, 1)
    strays = [p.name for p in (data_root / SAVE_SUB / "0100").iterdir()]
    assert strays == []


def test_restore_rejects_a_member_escaping_the_save_dir(data_root):
    content = _archive({f"{SAVE_SUB}/../../.config/Eden/qt-config.ini": (b"x", time.time())})
    result = broker._extract_save_archive(content)
    assert isinstance(result, str) and "escapes save dir" in result


def test_restore_rejects_a_member_outside_the_save_subtrees(data_root):
    content = _archive({"keys/prod.keys": (b"x", time.time())})
    result = broker._extract_save_archive(content)
    assert isinstance(result, str) and "outside save subtrees" in result


def test_restore_rejects_a_body_that_is_not_a_zip(data_root):
    result = broker._extract_save_archive(b"not a zip at all")
    assert isinstance(result, str) and "not a zip" in result


# ── The HTTP endpoint ─────────────────────────────────────────────────────────


# Every reason for having no archive maps to a distinct status, and RomM acts on
# the difference: 404 means "nothing changed, you are in sync", 503 means "ask
# again shortly", 413 means "this will never succeed". Collapsing any pair would
# have RomM record a failed pull as a completed one.


def _armed(baseline):
    """Put the session in the state a launched game leaves behind."""
    with broker._session_lock:
        broker._session["save_baseline"] = baseline
        broker._session["rom_name"] = "Test Game"


def test_get_save_file_serves_the_archive(client, data_root):
    base, _ = client
    _save(data_root, "0100/game.dat", mtime=time.time())
    _armed(time.time() - 60)
    code, body, headers = request(base, "/save-file")
    assert code == 200
    assert headers["Content-Type"] == "application/zip"
    assert headers["X-Save-Filename"] == "Test Game.saves.zip"
    assert _members(body) == [f"{SAVE_SUB}/0100/game.dat"]


def test_get_save_file_is_404_before_any_launch(client, data_root):
    base, _ = client
    code, body, _ = request(base, "/save-file")
    assert code == 404
    assert body["error"] == "no game has been launched"


def test_get_save_file_is_404_when_nothing_changed(client, data_root):
    base, _ = client
    _save(data_root, "0100/game.dat", mtime=time.time() - 3600)
    _armed(time.time() - 60)
    assert request(base, "/save-file")[0] == 404


def test_get_save_file_is_413_when_the_changed_set_is_too_large(
    client, data_root, monkeypatch
):
    base, _ = client
    monkeypatch.setattr(broker, "SAVE_FILE_MAX_BYTES", 8)
    _save(data_root, "0100/big.dat", content=b"x" * 64, mtime=time.time())
    _armed(time.time() - 60)
    code, body, _ = request(base, "/save-file")
    assert code == 413
    assert "size limit" in body["error"]


def test_get_save_file_is_503_while_saves_are_mid_write(client, data_root, monkeypatch):
    base, _ = client
    _save(data_root, "0100/game.dat", mtime=time.time())
    monkeypatch.setattr(broker, "_read_file_stable", lambda p, **k: None)
    _armed(time.time() - 60)
    code, body, _ = request(base, "/save-file")
    assert code == 503
    assert "still being written" in body["error"]


def test_get_save_file_reports_partially_skipped_pulls(client, data_root, monkeypatch):
    """A pull that dropped a mid-write file still succeeds, but the caller has
    to be able to tell it was not the complete set."""
    base, _ = client
    now = time.time()
    _save(data_root, "0100/settled.dat", mtime=now)
    _save(data_root, "0100/busy.dat", mtime=now)
    real = broker._read_file_stable
    monkeypatch.setattr(
        broker, "_read_file_stable",
        lambda p, **k: None if p.name == "busy.dat" else real(p, **k),
    )
    _armed(now - 60)
    code, body, headers = request(base, "/save-file")
    assert code == 200
    assert headers["X-Save-Skipped-Unstable"] == "1"
    assert _members(body) == [f"{SAVE_SUB}/0100/settled.dat"]
