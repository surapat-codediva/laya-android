#!/bin/bash
# usage: compare_cli.sh <path/to/laya_android.py> [python]
# Wall time of CLI commands on the device's Settings app: screen, tap, key back (x3), pick (x2), check.
# Read-only except for navigating Settings -> Network & internet and back.
CLI="$1"
PY="${2:-$(dirname "$0")/../skills/laya-android/.venv/bin/python}"
now() { python3 -c 'import time; print(time.perf_counter())'; }
t() {
  local s=$(now); "$PY" "$CLI" "$@" >/tmp/cli_out.txt 2>/tmp/cli_err.txt; local rc=$?; local e=$(now)
  python3 -c "print('%-38s rc=%d %6.0f ms' % ('$*', $rc, ($e-$s)*1000))"
}
adb shell am force-stop com.android.settings; adb shell am start -W -a android.settings.SETTINGS >/dev/null; sleep 2.5
for i in 1 2 3; do
  t screen
  n=$(grep -o '^\[[0-9]*\] item "Network' /tmp/cli_out.txt | grep -o '[0-9]*' | head -1)
  t tap "$n"
  t key back
done
t pick "open network settings"
t pick "open network settings"
t check "is the Settings home page shown?"
