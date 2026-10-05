#!/usr/bin/env bash
# Proves the install works on this machine: bash tests/selftest.sh
# Takes the SyncNet authors' sample clip, shifts its audio by known amounts and checks
# that the measurement finds exactly those shifts (+-1 frame), then that `fix` brings a
# broken copy back to 0 and verifies itself.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.venv/bin:$PATH"
PY=.venv/bin/python
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
CLIP=tests/example.avi
[ -s "$CLIP" ] || curl -sfL -o "$CLIP" https://www.robots.ox.ac.uk/~vgg/software/lipsync/data/example.avi

ffmpeg -v error -y -i "$CLIP" -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac "$T/base.mp4"
ffmpeg -v error -y -i "$T/base.mp4" -c:v copy -af "adelay=300:all=1,apad,atrim=end=12.68" "$T/late300.mp4"
ffmpeg -v error -y -i "$T/base.mp4" -c:v copy -af "atrim=start=0.2,asetpts=PTS-STARTPTS,apad,atrim=end=12.68" "$T/early200.mp4"

off() { rm -f "$T/r.json"; "$PY" scripts/lipsync_fix.py measure "$1" --json "$T/r.json" >"$T/log" 2>&1 || true
        [ -s "$T/r.json" ] || { echo "MEASURE CRASHED:" >&2; tail -5 "$T/log" >&2; echo NA; return; }
        "$PY" -c "import json;print(json.load(open('$T/r.json'))['before'].get('offset_ms','NA'))"; }
b=$(off "$T/base.mp4"); l=$(off "$T/late300.mp4"); e=$(off "$T/early200.mp4")
echo "base ${b} ms | +300 shift -> ${l} ms | -200 shift -> ${e} ms"
"$PY" - "$b" "$l" "$e" <<'EOF'
import sys
if "NA" in sys.argv[1:]:
    print("MEASURE: FAIL (no result)"); sys.exit(1)
b, l, e = (float(x) for x in sys.argv[1:])
ok = abs((l - b) - 300) <= 40 and abs((e - b) + 200) <= 40
print("MEASURE: PASS" if ok else "MEASURE: FAIL"); sys.exit(0 if ok else 1)
EOF
if "$PY" scripts/lipsync_fix.py fix "$T/late300.mp4" -o "$T/fixed.mp4" 2>/dev/null | grep -q "^VERIFIED"; then
  echo "FIX: PASS (late copy repaired and re-measured within one frame)"
else
  echo "FIX: FAIL"; exit 1
fi
echo "selftest OK"
