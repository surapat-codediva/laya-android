#!/bin/bash
# Before/after measurements, interleaved so both versions share the host's load.
# usage: run_all.sh <before skill dir> <out dir>
#   e.g. git worktree add /tmp/laya-before 3c8eb84 && run_all.sh /tmp/laya-before/skills/laya-android /tmp/out
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
NEW="$HERE/../skills/laya-android"
OLD="$1"
OUT="$2"
PY="$NEW/.venv/bin/python"
mkdir -p "$OUT"
echo "== host"; sysctl -n hw.ncpu 2>/dev/null || nproc; uptime
echo "== A1 run loop, real Laya: before (in-process) / after (daemon)"
"$PY" "$HERE/compare_run.py" "$OLD" "$OUT/run_before_real.json" --runs 4 2>/dev/null
"$PY" "$HERE/compare_run.py" "$NEW" "$OUT/run_after_real.json" --runs 4 --daemon 2>/dev/null
echo "== A2 run loop, fake Laya (device side only)"
"$PY" "$HERE/compare_run.py" "$OLD" "$OUT/run_before_fake.json" --runs 4 --fake-laya 2>/dev/null
"$PY" "$HERE/compare_run.py" "$NEW" "$OUT/run_after_fake.json" --runs 4 --fake-laya 2>/dev/null
echo "== B CLI commands: before"
"$HERE/compare_cli.sh" "$OLD/laya_android.py" "$PY" | tee "$OUT/cli_before.txt"
echo "== B CLI commands: after"
"$HERE/compare_cli.sh" "$NEW/laya_android.py" "$PY" | tee "$OUT/cli_after.txt"
echo "== C la benchmark --step"
adb shell am force-stop com.android.settings; adb shell am start -W -a android.settings.SETTINGS >/dev/null; sleep 2.5
"$PY" "$NEW/laya_android.py" benchmark --iterations 20 --step --json > "$OUT/benchmark_after.json" 2>"$OUT/benchmark_after.txt"
cat "$OUT/benchmark_after.txt"
echo "== host"; uptime
