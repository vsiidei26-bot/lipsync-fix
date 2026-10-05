#!/usr/bin/env bash
# Deeper validation (5-10 min): bash tests/validate.sh
# Builds 50 s test videos from the authors' sample and checks the three hard cases:
#   drift   - audio slowly running away (0.4 % speed mismatch -> 200 ms over 50 s)
#   mixed   - two parts glued at a picture cut with opposite offsets (+200 / -200 ms)
#   tail    - audio early while the speaker talks to the very end (sound must not be lost)
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$PWD/.venv/bin:$PATH"
PY=.venv/bin/python
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
CLIP=tests/example.avi
[ -s "$CLIP" ] || curl -sfL -o "$CLIP" https://www.robots.ox.ac.uk/~vgg/software/lipsync/data/example.avi
ffmpeg -v error -y -stream_loop 3 -i "$CLIP" -c:v libx264 -crf 18 -pix_fmt yuv420p -r 25 -c:a aac -ar 48000 "$T/long.mp4"
D=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$T/long.mp4")
ffmpeg -v error -y -i "$T/long.mp4" -c:v copy -af "atempo=0.996,apad,atrim=end=$D" "$T/drift.mp4"
ffmpeg -v error -y -i "$T/long.mp4" -c:v copy -af "asplit[x][y];[x]atrim=0:25.36,asetpts=PTS-STARTPTS,adelay=200:all=1,atrim=end=25.36[p];[y]atrim=start=25.56,asetpts=PTS-STARTPTS,apad,atrim=end=$(python3 -c "print(round($D-25.36,3))")[q];[p][q]concat=n=2:v=0:a=1" "$T/mixed.mp4"
ffmpeg -v error -y -i "$T/long.mp4" -t 12.6 -c:v libx264 -crf 18 -af "atrim=start=0.3,asetpts=PTS-STARTPTS,apad,atrim=end=12.6" "$T/tail.mp4"

pass=0; fail=0
check() {  # name file expect_verdict
  "$PY" scripts/lipsync_fix.py fix "$2" -o "$T/$1_fixed.mp4" --json "$T/$1.json" > "$T/$1.log" 2>&1 || true
  r=$("$PY" - "$T/$1.json" "$3" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1])); want = sys.argv[2]
v = r["before"]["verdict"]; res = r.get("result")
ok = v == want and res == "fixed_verified"
print(("PASS" if ok else "FAIL") + f"  verdict={v} result={res} edge={r.get('extended')}")
EOF
)
  echo "$1: $r"; case "$r" in PASS*) pass=$((pass+1));; *) fail=$((fail+1)); tail -8 "$T/$1.log";; esac
}
check drift "$T/drift.mp4" drift
check mixed "$T/mixed.mp4" mixed
check tail  "$T/tail.mp4"  constant
# the tail case must have kept the sound by extending the picture
tl=$(python3 -c "import json;print(json.load(open('$T/tail.json')).get('extended',{}).get('tail',0))")
if python3 -c "import sys;sys.exit(0 if float('$tl')>0.2 else 1)"; then echo "tail-sound kept: PASS ($tl s frozen frame)"; pass=$((pass+1)); else echo "tail-sound kept: FAIL"; fail=$((fail+1)); fi
echo "validate: $pass passed, $fail failed"; [ "$fail" = 0 ]
