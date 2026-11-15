#!/usr/bin/env bash
# Run explicitly on the Linux host; never invoked during ordinary connection.
set -euo pipefail
umask 077
if [[ "${1:-}" == "--install" ]]; then
  sudo apt-get update
  sudo apt-get install -y python3-venv python3-pip tzdata xvfb xauth x11-utils xfce4 xfce4-terminal x11vnc novnc xdotool scrot dbus-x11 xdg-utils
  if ! command -v firefox >/dev/null && ! command -v firefox-esr >/dev/null && ! command -v chromium >/dev/null; then
    echo 'Install your distribution browser explicitly, for example: sudo apt-get install firefox-esr (Debian) or sudo snap install firefox (Ubuntu).'
  fi
fi
for cmd in python3 Xvfb xauth xdpyinfo startxfce4 x11vnc xdotool scrot; do
  command -v "$cmd" >/dev/null || { echo "Missing $cmd. Run bash bootstrap-desktop.sh --install" >&2; exit 1; }
done
display="${DUET_DISPLAY:-:99}"
state="${DUET_DESKTOP_DIR:-$HOME/.local/share/duet/desktop}"
mkdir -p "$state"
chmod 700 "$state"
export DISPLAY="$display"
export XAUTHORITY="$state/Xauthority"
if [[ ! -s "$XAUTHORITY" ]]; then
  touch "$XAUTHORITY"
  chmod 600 "$XAUTHORITY"
  # Cookie is supplied over stdin, never as a visible process argument.
  python3 - "$display" <<'PY' | xauth -f "$XAUTHORITY" source -
import secrets, sys
print('add ' + sys.argv[1] + ' MIT-MAGIC-COOKIE-1 ' + secrets.token_hex(16))
PY
fi
chmod 600 "$XAUTHORITY"
python3 - "$state" <<'PY'
import os, pathlib, secrets, string, subprocess, sys
root = pathlib.Path(sys.argv[1])
secret = root / 'vnc.secret'
encoded = root / 'vnc.pass'
if not secret.is_file() or not encoded.is_file():
    # VNC authentication is limited to eight bytes by the RFB protocol.
    password = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
    secret.write_text(password, encoding='ascii')
    secret.chmod(0o600)
    # The official interactive -storepasswd mode reads stdin; password never
    # appears in argv, environment, stdout or logs.
    subprocess.run(['x11vnc', '-storepasswd', str(encoded)],
                   input=(password + '\n' + password + '\ny\n').encode(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
for path in (secret, encoded):
    path.chmod(0o600)
PY

managed_pid() {
  python3 - "$1" "$2" "$3" <<'PY'
import os, pathlib, sys
try:
    pid = int(pathlib.Path(sys.argv[1]).read_text().strip())
    args = pathlib.Path('/proc/' + str(pid) + '/cmdline').read_bytes().split(b'\0')
    assert pid > 1 and pathlib.Path(os.fsdecode(args[0])).name == sys.argv[2]
    assert os.fsencode(sys.argv[3]) in args
except (OSError, ValueError, AssertionError):
    sys.exit(1)
PY
}

# Do not reuse a legacy unauthenticated display or kill unrelated processes.
if ! managed_pid "$state/xvfb.pid" Xvfb "$XAUTHORITY"; then
  if [[ -S "/tmp/.X11-unix/X${display#:}" ]]; then
    echo 'Display already exists without a verified duet authenticated pidfile. Stop that desktop explicitly or select DUET_DISPLAY.' >&2
    exit 1
  fi
  nohup Xvfb "$display" -screen 0 1600x1000x24 -nolisten tcp -auth "$XAUTHORITY" >"$state/xvfb.log" 2>&1 </dev/null &
  echo "$!" > "$state/xvfb.pid"
  for attempt in {1..30}; do
    xdpyinfo -display "$display" >/dev/null 2>&1 && break
    sleep 0.2
  done
fi
xdpyinfo -display "$display" >/dev/null 2>&1 || { echo 'Authenticated X display did not start.' >&2; exit 1; }
if ! pgrep -u "$(id -u)" -f 'xfce4-session' >/dev/null; then
  nohup dbus-run-session -- startxfce4 >"$state/xfce.log" 2>&1 </dev/null &
fi
if ! managed_pid "$state/vnc.pid" x11vnc "$state/vnc.pass"; then
  nohup x11vnc -display "$display" -auth "$XAUTHORITY" -localhost -rfbport 5900 -forever -shared -rfbauth "$state/vnc.pass" >"$state/vnc.log" 2>&1 </dev/null &
  echo "$!" > "$state/vnc.pid"
  sleep 0.3
  managed_pid "$state/vnc.pid" x11vnc "$state/vnc.pass" || { echo 'Authenticated VNC did not start; inspect desktop/vnc.log.' >&2; exit 1; }
fi
echo 'duet desktop started on loopback VNC port 5900. Check GET /desktop/status.'
