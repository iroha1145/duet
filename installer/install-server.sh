#!/usr/bin/env bash
# Root uses a dedicated unprivileged system account; other users use user units.
set -euo pipefail
umask 077
die() { printf 'Error: %s\n' "$*" >&2; exit 2; }
usage() {
  cat <<'EOF'
Usage: bash installer/install-server.sh --public-url https://duet.example.com [options]
  --root PATH       Install directory (root: /opt/duet; user: $HOME/.local/share/duet)
  --service-user NAME  Dedicated system account for root installs (default: duet-server)
  --source PATH     Unpacked server bundle (default: directory above installer)
  --username NAME   First account username (required only for a new account)
  --port NUMBER     Loopback server port (default: 48766)
  --service NAME    systemd unit basename (default: duet-server)
  --upgrade         Explicitly replace this install's existing service/release
  --desktop         Also install/start the optional desktop unit
  --no-systemd      Prepare files only; print the foreground startup command
  --check           Validate options/source/paths only; make no changes
  --help            Show this help
Root creates a dedicated service account and system units; user installs never use sudo.
Provider keys and TLS certificates are not managed by this script.
Install paths must be absolute ASCII paths without spaces; no symlink ancestors.
EOF
}
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_root="${HOME}/.local/share/duet"
scope=user service_user=''
if ((EUID == 0)); then install_root=/opt/duet; scope=system; service_user=duet-server; fi
public_url='' username='' port=48766 service=duet-server
upgrade=0 desktop=0 no_systemd=0 check=0
while (($#)); do
  case "$1" in
    --root|--source|--public-url|--username|--port|--service|--service-user)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die "Missing value for $1"
      case "$1" in
        --service-user) service_user=$2;; --root) install_root=$2;; --source) source_dir=$2;;
        --public-url) public_url=$2;; --username) username=$2;;
        --port) port=$2;; --service) service=$2;;
      esac
      shift 2;;
    --upgrade) upgrade=1; shift;; --desktop) desktop=1; shift;;
    --no-systemd) no_systemd=1; shift;; --check) check=1; shift;;
    --help|-h) usage; exit 0;; *) die "Unknown option: $1";;
  esac
done
[[ "$port" =~ ^[0-9]{1,5}$ ]] || die 'Port must be an integer from 1024 to 65535'
port=$((10#$port))
((port >= 1024 && port <= 65535)) || die 'Port must be from 1024 to 65535'
[[ "$service" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]] || die 'Invalid service name'
[[ -z "$username" || "$username" =~ ^[a-zA-Z0-9._@+-]{1,128}$ ]] || die 'Invalid account username'
[[ "$install_root" =~ ^/[a-zA-Z0-9_./-]+$ ]] || die 'Install root must be an absolute ASCII path without spaces'
[[ "$public_url" == https://* ]] || die '--public-url must be an HTTPS origin'
command -v python3 >/dev/null || die 'python3 is required'
python3 -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || die 'Python 3.11+ with venv is required'
[[ "$(uname -s)" == Linux ]] || die 'This installer requires Linux'
service_uid=$(id -u)
service_group=''
if [[ "$scope" == system ]]; then
  [[ "$service_user" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die 'Invalid dedicated service user'
  command -v runuser >/dev/null || die 'runuser (util-linux) is required'
  command -v setpriv >/dev/null || die 'setpriv (util-linux) is required'
  command -v useradd >/dev/null || die 'useradd is required'
  command -v getent >/dev/null || die 'getent is required'
  service_uid=-1
  if getent passwd "$service_user" >/dev/null; then
    service_uid=$(id -u "$service_user")
    ((service_uid != 0)) || die 'Service account must not be root'
    service_group=$(id -gn "$service_user")
  else
    ! getent group "$service_user" >/dev/null || die 'Dedicated group already exists without the service account'
  fi
else
  [[ -z "$service_user" ]] || die '--service-user requires root'
fi
as_service() {
  if [[ "$scope" == system ]]; then (cd -- "$release" && runuser -u "$service_user" -- setpriv --no-new-privs "$@"); else "$@"; fi
}
ctl() {
  if [[ "$scope" == system ]]; then systemctl "$@"; else systemctl --user "$@"; fi
}
verify_system_unit() {
  [[ "$scope" == system ]] || return 0
  local name=$1 expected=$2
  [[ $(ctl show "$name" --property=User --value) == "$service_user" &&
     $(ctl show "$name" --property=Group --value) == "$service_group" &&
     $(ctl show "$name" --property=NoNewPrivileges --value) == yes &&
     $(ctl show "$name" --property=FragmentPath --value) == "$expected" &&
     -z $(ctl show "$name" --property=DropInPaths --value) ]] || die 'Effective system unit identity or overrides are unsafe'
}
source_dir="$(cd -- "$source_dir" && pwd -P)" || die 'Source directory does not exist'
# Validate before creating files or contacting package indexes.
install_root="$(python3 - "$install_root" "$source_dir" "$public_url" "$scope" "$service_user" "$service_uid" <<'PY'
import os, pathlib, sys
from urllib.parse import urlsplit
root = pathlib.Path(os.path.abspath(sys.argv[1]))
source = pathlib.Path(sys.argv[2])
url = urlsplit(sys.argv[3])
scope, account, uid = sys.argv[4:]
try:
    assert url.scheme == 'https' and url.hostname and not url.username and not url.password
    assert url.path in ('', '/') and not url.query and not url.fragment
    assert not any(c.isspace() or ord(c) < 32 for c in sys.argv[3]) and '\\' not in sys.argv[3]
    assert url.port is None or 1 <= url.port <= 65535
    assert root not in (pathlib.Path('/'), pathlib.Path.home(), pathlib.Path('/home'), pathlib.Path('/tmp'))
    assert root != source and root not in source.parents and source not in root.parents
    for ancestor in (root, *root.parents):
        assert not ancestor.is_symlink(), f'Symlink ancestor: {ancestor}'
    if root.exists():
        assert root.is_dir() and root.stat().st_uid == os.getuid(), 'Install root must be owned by this user'
        assert not any(root.iterdir()) or (root / '.duet-install').is_file(), 'Nonempty root is not a duet installation'
        if scope == 'system' and any(root.iterdir()):
            marker = root / '.duet-install'
            assert not marker.is_symlink() and marker.stat().st_uid == 0 and not marker.stat().st_mode & 0o022
            assert marker.read_text() == f'duet system installation\nservice-user: {account}\n', 'Installation service account mismatch'
            assert uid != '-1', 'Installation service account is missing; repair account ownership before upgrading'

        if scope == 'system':
            assert not root.stat().st_mode & 0o022, 'System root must not be group/other writable'
            releases = root / 'releases'
            if releases.exists():
                assert not releases.is_symlink() and releases.stat().st_uid == 0 and not releases.stat().st_mode & 0o022, 'Unsafe releases directory'
        for name in ('.duet-install','releases','current','current.new','data','data/auth','data/auth/auth.sqlite3','data/service.pid','desktop'):
            path = root / name
            assert not path.is_symlink(), f'Symlink in managed installation path: {path}'
    if scope == 'system':
        assert root.parent.is_dir(), 'Create a root-owned searchable parent directory first'
        for ancestor in (root.parent, *root.parent.parents):
            if ancestor.exists():
                assert ancestor.stat().st_uid == 0 and not ancestor.stat().st_mode & 0o022 and ancestor.stat().st_mode & 0o001, 'Unsafe or inaccessible system installation ancestor'
        # Existing accounts must have been created for this exact installation.
        if uid != '-1':
            import pwd, grp
            entry = pwd.getpwnam(account)
            assert (root / '.duet-install').is_file(), 'Existing service account does not belong to this installation'
            assert entry.pw_dir == str(root / 'data') and entry.pw_shell in ('/usr/sbin/nologin', '/sbin/nologin'), 'Unexpected service account home/shell'
            assert entry.pw_gid != 0 and grp.getgrgid(entry.pw_gid).gr_name == account, 'Service account must have its own non-root group'
            assert len(os.getgrouplist(account, entry.pw_gid)) == 1, 'Service account has supplementary groups'
            if (root / 'data').exists():
                assert (root / 'data').stat().st_uid == entry.pw_uid, 'Data owner does not match service account'
        assert not str(root).startswith('/root/'), 'Root installation must be accessible outside /root'
    for name in ('bridge/server.py','bridge/auth.py','duet_server/app.py','requirements.txt','launch.py','bootstrap-desktop.sh','bridge/admin_assets/index.html'):
        path = source / name
        assert path.is_file() and not path.is_symlink(), f'Missing/unsafe bundle file: {name}'
    for directory in ('bridge','duet_server'):
        for path in (source / directory).rglob('*'):
            assert not path.is_symlink(), f'Symlink in source: {path}'
except (AssertionError, ValueError) as e:
    sys.exit(str(e) or 'Invalid URL or install path')
print(root)
PY
)" || die 'Preflight validation failed'
public_url="${public_url%/}"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [[ "$scope" == system ]]; then unit_dir=/etc/systemd/system; fi
python3 - "$unit_dir" "$scope" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
for item in (path, *path.parents):
    if item.is_symlink(): sys.exit('Symlink unit directory is unsafe')
    if sys.argv[2] == 'system' and item.exists():
        if item.stat().st_uid != 0 or item.stat().st_mode & 0o022:
            sys.exit('Unsafe system unit directory')
PY
unit_path="$unit_dir/$service.service"
if ((no_systemd == 0)); then
  command -v systemctl >/dev/null || die 'systemctl is unavailable; use --no-systemd'
  ctl show-environment >/dev/null 2>&1 || die 'systemd is unavailable; use --no-systemd'
  if [[ "$scope" == system ]]; then
    for base in /etc/systemd/system /run/systemd/system /usr/local/lib/systemd/system /usr/lib/systemd/system /lib/systemd/system; do
      for name in "$service.service" "$service-desktop.service"; do
        [[ ! -e "$base/$name.d" && ! -L "$base/$name.d" ]] || die 'External systemd drop-ins are not supported for managed units'
      done
    done
  fi
  if [[ -e "$unit_path" || -L "$unit_path" ]]; then
    [[ ! -L "$unit_path" ]] || die 'Existing service unit is a symlink'
    grep -Fxq "# duet-install-root: $install_root" "$unit_path" || die 'Existing unit belongs to another installation'
    if [[ "$scope" == system ]]; then
      [[ $(stat -c %u "$unit_path") == 0 ]] && grep -Fxq "User=$service_user" "$unit_path" || die 'Invalid system unit owner/account'
    fi
    [[ -f "$install_root/.duet-install" ]] || die 'Unit exists without matching installation'
    verify_system_unit "$service.service" "$unit_path"
    ((upgrade)) || die 'Existing service requires explicit --upgrade'
  elif ctl cat "$service.service" >/dev/null 2>&1; then
    die 'Service name already exists outside this installer; choose --service'
  fi
  if ((desktop)); then
    desktop_path="$unit_dir/$service-desktop.service"
    if [[ -e "$desktop_path" || -L "$desktop_path" ]]; then
      [[ ! -L "$desktop_path" ]] && grep -Fxq "# duet-install-root: $install_root" "$desktop_path" || die 'Existing desktop unit belongs to another installation'
      if [[ "$scope" == system ]]; then
        [[ $(stat -c %u "$desktop_path") == 0 ]] && grep -Fxq "User=$service_user" "$desktop_path" || die 'Invalid desktop unit owner/account'
      fi
    elif ctl cat "$service-desktop.service" >/dev/null 2>&1; then
      die 'Desktop service name is already in use'
    fi
  fi
fi
if [[ -e "$install_root/current" ]] && ((upgrade == 0)); then
  die 'Existing release requires explicit --upgrade'
fi
account_exists=0
if [[ -f "$install_root/data/auth/auth.sqlite3" ]]; then
  account_exists="$(python3 - "$install_root/data/auth/auth.sqlite3" <<'PY'
import contextlib
import errno
import fcntl
import os
import pathlib
import sqlite3
import stat
import sys
import tempfile
import time

source = pathlib.Path(sys.argv[1])
SHARED_FIRST, SHARED_SIZE = 0x40000002, 510
WAL_LOCKS, WAL_DEADMAN = 120, 128
LIMIT = 128 * 1024 * 1024

def open_original(path, flags):
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(descriptor, 'r+b' if flags == os.O_RDWR else 'rb')
    info = os.fstat(descriptor)
    named = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
        stream.close()
        raise RuntimeError('Unsafe or changed account database file')
    return stream

def lock(stream, operation, length, offset):
    try:
        fcntl.lockf(stream, operation, length, offset)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise BlockingIOError from None
        raise

def unchanged(stream, path, original):
    opened, named = os.fstat(stream.fileno()), os.stat(path, follow_symlinks=False)
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if identity(opened) != identity(original) or identity(named) != identity(original):
        raise RuntimeError('Account database changed during snapshot')

def copy_bounded(stream, path, target):
    before = os.fstat(stream.fileno())
    if before.st_size > LIMIT:
        raise RuntimeError('Account database snapshot exceeds 128 MiB file limit')
    remaining = before.st_size
    with target.open('xb') as output:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError('Account database shortened during snapshot')
            output.write(block)
            remaining -= len(block)
        if stream.read(1):
            raise RuntimeError('Account database grew during snapshot')
    unchanged(stream, path, before)

# Default Linux SQLite unix VFS byte locks, documented in walformat.html and
# https://www.sqlite.org/walformat.html#wal_locks
# https://github.com/sqlite/sqlite/blob/master/src/os.h
# https://github.com/sqlite/sqlite/blob/master/src/os_unix.c
# Never connect SQLite to the original database:
# mode=ro may create sidecars. Main SHARED prevents last-connection cleanup;
# WAL deadman + write/checkpoint/recovery locks stabilize existing WAL files.
with tempfile.TemporaryDirectory(prefix='duet-account-check-') as directory:
    snapshot = pathlib.Path(directory) / 'auth.sqlite3'
    for attempt in range(50):
        with contextlib.ExitStack() as stack:
            main = stack.enter_context(open_original(source, os.O_RDWR))
            shm = None
            try:
                try:
                    lock(main, fcntl.LOCK_EX | fcntl.LOCK_NB, SHARED_SIZE, SHARED_FIRST)
                except BlockingIOError:
                    lock(main, fcntl.LOCK_SH | fcntl.LOCK_NB, 1, 0x40000000)
                    lock(main, fcntl.LOCK_SH | fcntl.LOCK_NB, SHARED_SIZE, SHARED_FIRST)
                    lock(main, fcntl.LOCK_UN, 1, 0x40000000)
                    shm_path = str(source) + '-shm'
                    shm = stack.enter_context(open_original(shm_path, os.O_RDONLY))
                    lock(shm, fcntl.LOCK_SH | fcntl.LOCK_NB, 1, WAL_DEADMAN)
                    lock(shm, fcntl.LOCK_SH | fcntl.LOCK_NB, 3, WAL_LOCKS)
                    shm_before = os.fstat(shm.fileno())
                    unchanged(shm, shm_path, shm_before)
            except (BlockingIOError, FileNotFoundError):
                time.sleep(0.02)
                continue
            journal = pathlib.Path(str(source) + '-journal')
            if journal.exists() and journal.stat().st_size:
                raise RuntimeError('Account rollback journal needs recovery before preflight')
            copy_bounded(main, source, snapshot)
            wal_path = str(source) + '-wal'
            try:
                wal = stack.enter_context(open_original(wal_path, os.O_RDONLY))
            except FileNotFoundError:
                pass
            else:
                copy_bounded(wal, wal_path, pathlib.Path(str(snapshot) + '-wal'))
            if shm is not None:
                unchanged(shm, shm_path, shm_before)
            break
    else:
        raise RuntimeError('Account database is busy; retry preflight after writes finish')
    # SQLite may modify only the temporary copy, which includes committed WAL.
    with sqlite3.connect(snapshot, timeout=1) as db:
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        if db.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise RuntimeError('Account database is corrupt')
        print(int(db.execute('SELECT 1 FROM account WHERE singleton=1').fetchone() is not None))
PY
)" || die 'Cannot safely inspect existing account database'
fi
if ((account_exists == 0)); then
  [[ -n "$username" ]] || die '--username is required for the first account'
  ((check)) || [[ -t 0 && -t 1 ]] || die 'First account setup requires an interactive terminal for getpass'
fi
if ((desktop)); then
  for command in Xvfb xauth xdpyinfo startxfce4 x11vnc xdotool scrot dbus-run-session; do
    command -v "$command" >/dev/null || die "Optional desktop requires $command; install prerequisites first"
  done
fi
if [[ ! -e "$install_root/current" ]]; then
  python3 - "$port" <<'PY'
import socket, sys
with socket.socket() as sock:
    try: sock.bind(('127.0.0.1', int(sys.argv[1])))
    except OSError: sys.exit('Loopback port is already in use; choose --port')
PY
fi
if ((check)); then
  printf 'Validation passed. No files or services changed.\n'; exit 0
fi
if [[ "$scope" == system && "$service_uid" == -1 ]]; then
  useradd --system --user-group --home-dir "$install_root/data" --no-create-home --shell /usr/sbin/nologin "$service_user"
  service_group=$(id -gn "$service_user")
  [[ $(id -u "$service_user") != 0 ]] || die 'Service account must not be root'
fi
mkdir -p -- "$install_root/releases" "$install_root/data"
chmod 700 "$install_root" "$install_root/data"
if [[ "$scope" == system ]]; then
  chmod 711 "$install_root"
  chmod 711 "$install_root/releases"
  chown "$service_user:$service_group" "$install_root/data"
  if ((desktop)); then mkdir -p "$install_root/desktop"; chown "$service_user:$service_group" "$install_root/desktop"; fi
  printf 'duet system installation\nservice-user: %s\n' "$service_user" > "$install_root/.duet-install"
else
  printf 'duet user installation\n' > "$install_root/.duet-install"
fi
release="$(mktemp -d "$install_root/releases/release-XXXXXXXX")"
trap 'printf "Installation stopped. Existing data was preserved; staged release: %s\n" "$release" >&2' ERR
python3 - "$source_dir" "$release" <<'PY'
import pathlib, shutil, sys
source, target = map(pathlib.Path, sys.argv[1:])
for directory in ('bridge','duet_server'):
    (target / directory).mkdir()
    for path in (source / directory).glob('*.py'):
        shutil.copy2(path, target / directory / path.name)
shutil.copytree(source / 'bridge/admin_assets', target / 'bridge/admin_assets')
shutil.copytree(source / 'bridge/prompt_assets', target / 'bridge/prompt_assets')
for name in ('requirements.txt','bootstrap-desktop.sh','launch.py'):
    shutil.copy2(source / name, target / name)
PY
if [[ "$scope" == system ]]; then
  chown -R "$service_user:$service_group" "$release"
fi
as_service python3 -m venv "$release/venv"
as_service "$release/venv/bin/python" -m pip install --disable-pip-version-check -r "$release/requirements.txt"
as_service "$release/venv/bin/python" -m compileall -q "$release/bridge" "$release/duet_server"
if ((account_exists == 0)); then
  (cd -- "$release" && as_service "$release/venv/bin/python" -m bridge.auth init --data-dir "$install_root/data" --username "$username")
else
  printf 'Existing account retained; password and device sessions were not reset.\n'
fi
# The current pointer is plain text, avoiding symlink traversal during later installs.
old_release=''
[[ ! -f "$install_root/current" ]] || old_release="$(cat "$install_root/current")"
if ((no_systemd == 0)); then
  mkdir -p -- "$unit_dir"
  # Unit values contain only prevalidated paths/origin. Percent must be escaped for systemd.
  python3 - "$unit_dir" "$service" "$install_root" "$release" "$public_url" "$port" "$desktop" "$scope" "$service_user" "$service_group" <<'PY'
import os, pathlib, sys
units, service, root, release, origin, port, desktop, scope, user, group = sys.argv[1:]
identity = f'User={user}\nGroup={group}\nNoNewPrivileges=yes\n' if scope == 'system' else ''
target = 'multi-user.target' if scope == 'system' else 'default.target'
def quote(value):
    return '"' + value.replace('\\','\\\\').replace('"','\\"').replace('%','%%') + '"'
def save(name, text):
    path = pathlib.Path(units) / name
    if path.is_symlink():
        raise SystemExit('Refusing symlink unit: ' + str(path))
    temporary = path.with_suffix('.service.new')
    with temporary.open('x', encoding='utf-8') as f: f.write(text)
    os.replace(temporary, path)
desktop_unit = service + '-desktop.service'
dependency = f'Wants={desktop_unit}\nAfter={desktop_unit}\n' if desktop == '1' else ''
save(service + '.service', f'''# duet-install-root: {root}
[Unit]
Description=duet single-account server
After=network.target
{dependency}
[Service]
Type=simple
{identity}WorkingDirectory={release}
ExecStart={quote(release+'/venv/bin/python')} -m bridge.server --host 127.0.0.1 --port {port} --data-dir {quote(root+'/data')} --public-url {quote(origin)} --pid-file {quote(root+'/data/service.pid')}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0077
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy={target}
''')
if desktop == '1':
    path = pathlib.Path(units) / desktop_unit
    if path.exists() and f'# duet-install-root: {root}\n' not in path.read_text():
        raise SystemExit('Existing desktop unit belongs to another installation')
    save(desktop_unit, f'''# duet-install-root: {root}
[Unit]
Description=duet optional authenticated desktop

[Service]
Type=oneshot
{identity}RemainAfterExit=yes
WorkingDirectory={release}
Environment={quote('DUET_DESKTOP_DIR='+root+'/desktop')}
ExecStart=/bin/bash {quote(release+'/bootstrap-desktop.sh')}
KillMode=control-group
TimeoutStartSec=60
UMask=0077

[Install]
WantedBy={target}
''')
PY
  ctl daemon-reload
  verify_system_unit "$service.service" "$unit_path"
  if ((desktop)); then verify_system_unit "$service-desktop.service" "$unit_dir/$service-desktop.service"; fi
  if ((desktop)); then ctl enable --now "$service-desktop.service"; fi
  ctl enable "$service.service"
  ctl restart "$service.service"
  ctl is-active --quiet "$service.service"
fi
printf '%s\n' "$release" > "$install_root/current.new"
mv -- "$install_root/current.new" "$install_root/current"
printf 'Installed release: %s\nPrevious release: %s\n' "$release" "${old_release:-none}"
if ((no_systemd)); then
  prefix=()
  [[ "$scope" != system ]] || prefix=(runuser -u "$service_user" -- setpriv --no-new-privs)
  printf 'Foreground start (keep this terminal open):\ncd %q && ' "$release"
  if ((${#prefix[@]})); then printf '%q ' "${prefix[@]}"; fi
  printf '%q -m bridge.server --host 127.0.0.1 --port %q --data-dir %q --public-url %q --pid-file %q\n' "$release/venv/bin/python" "$port" "$install_root/data" "$public_url" "$install_root/data/service.pid"
else
  printf 'Service started. Check: systemctl %sstatus %s.service\n' "$([[ $scope == user ]] && printf '%s ' --user)" "$service"
fi
printf 'Set up HTTPS, then open %s/duet/admin and configure a model.\n' "${public_url%/}"
