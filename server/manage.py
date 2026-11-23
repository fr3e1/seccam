"""Admin CLI: python -m server.manage [--allow-root] <cmd>."""
import argparse
import getpass
import os
import stat
import sys
import time
from typing import Optional

from . import auth
from .config import load_config
from .db import DB

ROOT_MSG = ("run as the service user: sudo -u seccam env $(cat /etc/seccam/seccam.env | xargs) "
            "python -m server.manage ...")


class CliError(Exception):
    """A runtime error: message to stderr, exit code 1."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        raise SystemExit(2)


def _geteuid() -> int:
    return os.geteuid()


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m server.manage")
    parser.add_argument("--allow-root", action="store_true", help="allow running as root (drops to the data dir owner first)")
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    for name in ("create-admin", "reset-password"):
        p = sub.add_parser(name)
        p.add_argument("--username", required=True)
        p.add_argument("--password-stdin", action="store_true")
    p = sub.add_parser("disable-totp")
    p.add_argument("--username", required=True)
    p = sub.add_parser("revoke-sessions")
    p.add_argument("--username")
    sub.add_parser("list-devices")
    p = sub.add_parser("revoke-device")
    p.add_argument("id")
    sub.add_parser("setup-token")
    return parser


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        line = sys.stdin.readline()
        if not line:
            raise CliError("no password on stdin")
        return line.rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise CliError("passwords do not match")
    return first


def _require_user(db: DB, username: str) -> dict:
    user = db.get_user_by_username(username)
    if user is None:
        raise CliError(f"no such user: {username}")
    return user


def _drop_privileges() -> None:
    """As root, become the owner of data_dir before touching any file in it (no root writes into a
    directory the service user controls, so planted symlinks cannot redirect them)."""
    raw = os.environ.get("SECCAM_DATA_DIR", "").strip() or "data"
    path = os.path.abspath(raw)
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise CliError(f"data dir {path}: {exc.strerror or exc}")
    if not stat.S_ISDIR(st.st_mode):
        raise CliError(f"data dir {path} must be a real directory (not a symlink)")
    if st.st_uid == 0:
        return
    os.setgroups([])
    os.setgid(st.st_gid)
    os.setuid(st.st_uid)


def _run(args, db: DB, data_dir) -> None:
    cmd = args.cmd
    if cmd == "create-admin":
        try:
            username = auth.validate_username(args.username)
            password = _read_password(args.password_stdin)
            auth.validate_password(password, username)
        except ValueError as exc:
            raise CliError(str(exc))
        try:
            created = db.create_first_user(username, auth.hash_password(password))
        except ValueError as exc:
            raise CliError(str(exc))
        if created is None:
            raise CliError("already set up: a user exists")
        auth.clear_setup_token(data_dir)
        print(f"created {username}")
    elif cmd == "reset-password":
        user = _require_user(db, args.username)
        password = _read_password(args.password_stdin)
        try:
            auth.validate_password(password, user["username"])
        except ValueError as exc:
            raise CliError(str(exc))
        db.update_user(user["id"], password_hash=auth.hash_password(password),
                       password_changed_at=int(time.time() * 1000))
        db.delete_user_sessions(user["id"])
        print(f"password reset for {user['username']}")
    elif cmd == "disable-totp":
        user = _require_user(db, args.username)
        db.update_user(user["id"], totp_secret=None, totp_enabled=False, totp_last_step=0, recovery_codes=[])
        print(f"2fa disabled for {user['username']}")
    elif cmd == "revoke-sessions":
        if args.username:
            ids = [_require_user(db, args.username)["id"]]
        else:
            with db._lock:  # no list_users() in the DB API; read-only query
                ids = [r[0] for r in db._conn.execute("SELECT id FROM users").fetchall()]
        print(f"revoked {sum(db.delete_user_sessions(i) for i in ids)} sessions")
    elif cmd == "list-devices":
        for d in db.list_devices():
            seen = d["last_seen_at"] if d["last_seen_at"] is not None else "-"
            print(f"{d['id']}\t{d['kind']}\t{d['name']}\t{seen}")
    elif cmd == "revoke-device":
        if not db.delete_device(args.id):
            raise CliError("no such device")
        print(f"revoked {args.id}")
    elif cmd == "setup-token":
        if db.count_users() > 0:
            raise CliError("already set up")
        print(auth.ensure_setup_token(data_dir))


def main(argv: Optional[list[str]] = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if _geteuid() == 0 and not args.allow_root:
        print(ROOT_MSG, file=sys.stderr)
        return 2
    try:
        if _geteuid() == 0:
            _drop_privileges()
        cfg = load_config()
        db = DB(cfg.data_dir)
        _run(args, db, cfg.data_dir)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
