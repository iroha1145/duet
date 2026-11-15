#!/usr/bin/env bash
# Run as the unprivileged Linux user who will own the service and data.
set -euo pipefail
umask 077
die() { printf 'Error: %s\n' "$*" >&2; exit 2; }
usage() {
  cat <<'EOF'
Usage: bash installer/install-server.sh --public-url https://duet.example.com [options]
  --root PATH       Install directory (default: $HOME/.local/share/duet)
  --source PATH     Unpacked server bundle (default: directory above installer)
  --username NAME   First account username (required only for a new account)
  --port NUMBER     Loopback server port (default: 48766)
  --service NAME    systemd user unit basename (default: duet-server)
  --upgrade         Explicitly replace this install's existing service/release
  --desktop         Also install/start the optional desktop user unit
  --no-systemd      Prepare files only; print the foreground startup command
  --check           Validate options/source/paths only; make no changes
  --help            Show this help
No sudo, passwords, provider keys, or TLS certificates are managed by this script.
Install paths must be absolute ASCII paths without spaces; no symlink ancestors.
EOF
}
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_root="${HOME}/.local/share/duet"
public_url='' username='' port=48766 service=duet-server
upgrade=0 desktop=0 no_systemd=0 check=0
while (($#)); do
  case "$1" in
    --root|--source|--public-url|--username|--port|--service)
      (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || die "Missing value for $1"
      case "$1" in
        --root) install_root=$2;; --source) source_dir=$2;;
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
((EUID != 0)) || die 'Run as an unprivileged service user, not root'
source_dir="$(cd -- "$source_dir" && pwd -P)" || die 'Source directory does not exist'
# Validate before creating files or contacting package indexes.
install_root="$(python3 - "$install_root" "$source_dir" "$public_url" <<'PY'
import os, pathlib, sys
from urllib.parse import urlsplit
root = pathlib.Path(os.path.abspath(sys.argv[1]))
source = pathlib.Path(sys.argv[2])
url = urlsplit(sys.argv[3])
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
        for name in ('.duet-install','releases','current','current.new','data','data/auth','data/auth/auth.sqlite3','data/service.pid','desktop'):
            path = root / name
            assert not path.is_symlink(), f'Symlink in managed installation path: {path}'
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
unit_path="$unit_dir/$service.service"
if ((no_systemd == 0)); then
  command -v systemctl >/dev/null || die 'systemctl is unavailable; use --no-systemd'
  systemctl --user show-environment >/dev/null 2>&1 || die 'User systemd is unavailable; use --no-systemd'
  if [[ -e "$unit_path" ]]; then
    [[ ! -L "$unit_path" ]] || die 'Existing service unit is a symlink'
    grep -Fxq "# duet-install-root: $install_root" "$unit_path" || die 'Existing unit belongs to another installation'
    ((upgrade)) || die 'Existing service requires explicit --upgrade'
  elif systemctl --user cat "$service.service" >/dev/null 2>&1; then
    die 'Service name already exists outside this installer; choose --service'
  fi
  if ((desktop)); then
    desktop_path="$unit_dir/$service-desktop.service"
    if [[ -e "$desktop_path" || -L "$desktop_path" ]]; then
      [[ ! -L "$desktop_path" ]] && grep -Fxq "# duet-install-root: $install_root" "$desktop_path" || die 'Existing desktop unit belongs to another installation'
    elif systemctl --user cat "$service-desktop.service" >/dev/null 2>&1; then
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
import pathlib, sqlite3, sys
with sqlite3.connect(pathlib.Path(sys.argv[1]).as_uri() + '?mode=ro', uri=True) as db:
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
if ((check)); then
  printf 'Validation passed. No files or services changed.\n'; exit 0
fi
mkdir -p -- "$install_root/releases" "$install_root/data"
chmod 700 "$install_root" "$install_root/data"
printf 'duet user installation\n' > "$install_root/.duet-install"
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
python3 -m venv "$release/venv"
"$release/venv/bin/python" -m pip install --disable-pip-version-check -r "$release/requirements.txt"
"$release/venv/bin/python" -m compileall -q "$release/bridge" "$release/duet_server"
if ((account_exists == 0)); then
  (cd -- "$release" && "$release/venv/bin/python" -m bridge.auth init --data-dir "$install_root/data" --username "$username")
else
  printf 'Existing account retained; password and device sessions were not reset.\n'
fi
# The current pointer is plain text, avoiding symlink traversal during later installs.
old_release=''
[[ ! -f "$install_root/current" ]] || old_release="$(cat "$install_root/current")"
if ((no_systemd == 0)); then
  mkdir -p -- "$unit_dir"
  # Unit values contain only prevalidated paths/origin. Percent must be escaped for systemd.
  python3 - "$unit_dir" "$service" "$install_root" "$release" "$public_url" "$port" "$desktop" <<'PY'
import os, pathlib, sys
units, service, root, release, origin, port, desktop = sys.argv[1:]
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
WorkingDirectory={release}
ExecStart={quote(release+'/venv/bin/python')} -m bridge.server --host 127.0.0.1 --port {port} --data-dir {quote(root+'/data')} --public-url {quote(origin)} --pid-file {quote(root+'/data/service.pid')}
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0077
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
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
RemainAfterExit=yes
WorkingDirectory={release}
Environment={quote('DUET_DESKTOP_DIR='+root+'/desktop')}
ExecStart=/bin/bash {quote(release+'/bootstrap-desktop.sh')}
KillMode=control-group
TimeoutStartSec=60
UMask=0077

[Install]
WantedBy=default.target
''')
PY
  systemctl --user daemon-reload
  if ((desktop)); then systemctl --user enable --now "$service-desktop.service"; fi
  systemctl --user enable "$service.service"
  systemctl --user restart "$service.service"
  systemctl --user is-active --quiet "$service.service"
fi
printf '%s\n' "$release" > "$install_root/current.new"
mv -- "$install_root/current.new" "$install_root/current"
printf 'Installed release: %s\nPrevious release: %s\n' "$release" "${old_release:-none}"
if ((no_systemd)); then
  printf 'Foreground start (keep this terminal open):\ncd %q && %q -m bridge.server --host 127.0.0.1 --port %q --data-dir %q --public-url %q --pid-file %q\n' "$release" "$release/venv/bin/python" "$port" "$install_root/data" "$public_url" "$install_root/data/service.pid"
else
  printf 'Service started. Check: systemctl --user status %s.service\n' "$service"
fi
printf 'Set up HTTPS, then open %s/duet/admin and configure a model.\n' "${public_url%/}"
