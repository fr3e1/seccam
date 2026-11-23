import io
import os
import time

import pytest

from server import auth, manage
from server.db import DB


@pytest.fixture(autouse=True)
def _not_root(monkeypatch):
    monkeypatch.setattr(manage, "_geteuid", lambda: 1000)


def run(monkeypatch, capsys, argv, stdin=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = manage.main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def mkuser(db, name="alice", pw="correct horse"):
    return db.create_user(name, auth.hash_password(pw))


def test_create_admin(env, monkeypatch, capsys, cfg):
    auth.ensure_setup_token(cfg.data_dir)
    code, out, _ = run(monkeypatch, capsys, ["create-admin", "--username", "alice", "--password-stdin"],
                       "correct horse\n")
    assert (code, out) == (0, "created alice\n")
    db = DB(cfg.data_dir)
    user = db.get_user_by_username("ALICE")
    assert user and auth.verify_password("correct horse", user["password_hash"])
    assert auth.read_setup_token(cfg.data_dir) is None


def test_create_admin_fails_when_user_exists(env, monkeypatch, capsys, db):
    mkuser(db, "bob")
    code, out, err = run(monkeypatch, capsys, ["create-admin", "--username", "alice", "--password-stdin"],
                         "correct horse\n")
    assert code == 1 and out == "" and "already set up" in err
    assert db.count_users() == 1


@pytest.mark.parametrize("user,pw,msg", [("a", "correct horse", "username must be"),
                                         ("alice", "short", "at least 10"), ("alice", "", "at least 10")])
def test_create_admin_validation(env, monkeypatch, capsys, db, user, pw, msg):
    code, _, err = run(monkeypatch, capsys, ["create-admin", "--username", user, "--password-stdin"], pw + "\n")
    assert code == 1 and msg in err
    assert db.count_users() == 0


def test_create_admin_empty_stdin(env, monkeypatch, capsys):
    code, _, err = run(monkeypatch, capsys, ["create-admin", "--username", "alice", "--password-stdin"], "")
    assert code == 1 and "no password" in err


def test_create_admin_getpass_twice(env, monkeypatch, capsys, db):
    answers = iter(["correct horse", "correct horse"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert run(monkeypatch, capsys, ["create-admin", "--username", "alice"])[0] == 0
    assert db.count_users() == 1


def test_create_admin_getpass_mismatch(env, monkeypatch, capsys, db):
    answers = iter(["correct horse", "other horse!!"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    code, _, err = run(monkeypatch, capsys, ["create-admin", "--username", "alice"])
    assert code == 1 and "do not match" in err and db.count_users() == 0


def test_reset_password(env, monkeypatch, capsys, db):
    user = mkuser(db)
    db.create_session("s1", user["id"], "c", int(time.time() * 1000) + 10**6, None, None)
    code, out, _ = run(monkeypatch, capsys, ["reset-password", "--username", "alice", "--password-stdin"],
                       "brand new pass\n")
    assert (code, out) == (0, "password reset for alice\n")
    fresh = db.get_user(user["id"])
    assert auth.verify_password("brand new pass", fresh["password_hash"])
    assert db.get_session("s1") is None


def test_reset_password_errors(env, monkeypatch, capsys, db):
    code, _, err = run(monkeypatch, capsys, ["reset-password", "--username", "ghost", "--password-stdin"], "x\n")
    assert code == 1 and "no such user" in err
    mkuser(db)
    code, _, err = run(monkeypatch, capsys, ["reset-password", "--username", "alice", "--password-stdin"], "short\n")
    assert code == 1 and "at least 10" in err


def test_disable_totp(env, monkeypatch, capsys, db):
    user = mkuser(db)
    db.update_user(user["id"], totp_secret="ABC", totp_enabled=True, totp_last_step=5, recovery_codes=["h"])
    code, out, _ = run(monkeypatch, capsys, ["disable-totp", "--username", "alice"])
    assert (code, out) == (0, "2fa disabled for alice\n")
    u = db.get_user(user["id"])
    assert (u["totp_secret"], u["totp_enabled"], u["recovery_codes"]) == (None, False, [])
    assert run(monkeypatch, capsys, ["disable-totp", "--username", "ghost"])[0] == 1


def test_revoke_sessions(env, monkeypatch, capsys, db):
    a, b = mkuser(db, "alice"), mkuser(db, "bobby")
    exp = int(time.time() * 1000) + 10**6
    for sid, u in (("s1", a), ("s2", a), ("s3", b)):
        db.create_session(sid, u["id"], "c", exp, None, None)
    assert run(monkeypatch, capsys, ["revoke-sessions", "--username", "alice"])[1] == "revoked 2 sessions\n"
    assert db.get_session("s3") is not None
    assert run(monkeypatch, capsys, ["revoke-sessions"])[1] == "revoked 1 sessions\n"
    assert run(monkeypatch, capsys, ["revoke-sessions"])[1] == "revoked 0 sessions\n"
    assert run(monkeypatch, capsys, ["revoke-sessions", "--username", "ghost"])[0] == 1


def test_devices(env, monkeypatch, capsys, db):
    assert run(monkeypatch, capsys, ["list-devices"]) == (0, "", "")
    db.create_device("aaaaaaaaaaaa", "Porch", "agent", "h")
    db.create_device("bbbbbbbbbbbb", "Phone", "browser", "h")
    db.touch_device("bbbbbbbbbbbb", 1234, "1.1.1.1")
    code, out, _ = run(monkeypatch, capsys, ["list-devices"])
    assert code == 0
    assert out.splitlines() == ["aaaaaaaaaaaa\tagent\tPorch\t-", "bbbbbbbbbbbb\tbrowser\tPhone\t1234"]
    assert run(monkeypatch, capsys, ["revoke-device", "aaaaaaaaaaaa"])[:2] == (0, "revoked aaaaaaaaaaaa\n")
    code, _, err = run(monkeypatch, capsys, ["revoke-device", "aaaaaaaaaaaa"])
    assert code == 1 and "no such device" in err


def test_setup_token(env, monkeypatch, capsys, db, cfg):
    code, out, _ = run(monkeypatch, capsys, ["setup-token"])
    assert code == 0 and out.strip() == auth.read_setup_token(cfg.data_dir)
    mkuser(db)
    code, out, err = run(monkeypatch, capsys, ["setup-token"])
    assert code == 1 and out == "" and "already set up" in err


@pytest.mark.parametrize("argv", [[], ["bogus"], ["create-admin"], ["revoke-device"], ["reset-password"],
                                  ["--nope", "list-devices"]])
def test_usage_errors_exit_2(env, monkeypatch, capsys, argv):
    assert run(monkeypatch, capsys, argv)[0] == 2


def test_invalid_config_exit_1(env, monkeypatch, capsys):
    monkeypatch.setenv("SECCAM_HTTP_PORT", "notanumber")
    code, _, err = run(monkeypatch, capsys, ["list-devices"])
    assert code == 1 and "SECCAM_HTTP_PORT" in err


def test_root_refused(env, monkeypatch, capsys, db):
    monkeypatch.setattr(manage, "_geteuid", lambda: 0)
    code, out, err = run(monkeypatch, capsys, ["list-devices"])
    assert code == 2 and out == ""
    assert err.strip() == manage.ROOT_MSG
    assert "sudo -u seccam env $(cat /etc/seccam/seccam.env | xargs) python -m server.manage" in err


def _fake_root(monkeypatch):
    calls = []
    monkeypatch.setattr(manage, "_geteuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda g: calls.append(("groups", list(g))))
    monkeypatch.setattr(os, "setgid", lambda g: calls.append(("gid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: calls.append(("uid", u)))
    return calls


def test_root_allowed_drops_privileges_before_touching_data(env, monkeypatch, capsys, cfg):
    st = os.stat(cfg.data_dir)
    if st.st_uid == 0:
        pytest.skip("data dir owned by root")
    calls = _fake_root(monkeypatch)
    chowns = []
    monkeypatch.setattr(os, "chown", lambda *a, **k: chowns.append(a))
    seen = []
    orig = manage.load_config
    monkeypatch.setattr(manage, "load_config", lambda: (seen.append(list(calls)), orig())[1])
    code, _, _ = run(monkeypatch, capsys, ["--allow-root", "list-devices"])
    assert code == 0
    assert calls == [("groups", []), ("gid", st.st_gid), ("uid", st.st_uid)]
    assert seen == [calls] and chowns == []


def test_root_refuses_symlinked_data_dir(env, monkeypatch, capsys, tmp_path):
    calls = _fake_root(monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("SECCAM_DATA_DIR", str(link))
    code, _, err = run(monkeypatch, capsys, ["--allow-root", "list-devices"])
    assert code == 1 and "real directory" in err and calls == []


def test_root_missing_data_dir(env, monkeypatch, capsys, tmp_path):
    calls = _fake_root(monkeypatch)
    monkeypatch.setenv("SECCAM_DATA_DIR", str(tmp_path / "nope"))
    code, _, err = run(monkeypatch, capsys, ["--allow-root", "list-devices"])
    assert code == 1 and calls == []


def test_non_root_never_drops(env, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(os, "setuid", lambda u: calls.append(u))
    assert run(monkeypatch, capsys, ["list-devices"])[0] == 0
    assert calls == []
