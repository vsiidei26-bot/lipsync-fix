#!/usr/bin/env bash
# lipsync-fix installer:  bash install.sh
# Works on a machine with NOTHING installed except curl + internet (macOS or Linux; Windows -> WSL).
# Everything goes INSIDE this folder: .tools/ (uv), .venv/ (Python 3.11 + packages + ffmpeg if the system has none).
# No sudo, no Homebrew, no admin password. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"
say() { printf "▸ %s\n" "$1"; }
has() { command -v "$1" >/dev/null 2>&1; }
has curl || { echo "curl is missing (Ubuntu/WSL: sudo apt-get install -y curl). It is the only requirement."; exit 1; }

say "uv (Python manager)"
export PATH="$PWD/.tools/bin:$PATH"
if ! has uv; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$PWD/.tools/bin" INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
fi
uv --version

say "Python 3.11 environment (.venv)"
export UV_PYTHON_INSTALL_DIR="$PWD/.tools/python"     # Python itself also lives inside this folder
export UV_CACHE_DIR="$PWD/.tools/cache"
if ! .venv/bin/python -c "import sys" >/dev/null 2>&1; then
  uv venv -q --clear --python 3.11 .venv               # (re)create: missing, moved, or broken environment
fi

say "Python packages — about 1.2 GB (macOS) / 1.8 GB (Linux) on disk, first time only"
EXTRA=""
# Linux: the default torch wheel bundles CUDA (several GB); the CPU wheel is all this skill needs.
[ "$(uname -s)" = "Linux" ] && EXTRA="--extra-index-url https://download.pytorch.org/whl/cpu --index-strategy unsafe-best-match"
# shellcheck disable=SC2086
uv pip install -q -p .venv/bin/python $EXTRA -r requirements.txt
# MediaPipe pulls the GUI build of OpenCV, which needs desktop libraries (libxcb, libGL) that servers / WSL / minimal
# Linux don't have. Swap it for the identical headless build (same cv2, no GUI).
uv pip uninstall -q -p .venv/bin/python opencv-python opencv-contrib-python 2>/dev/null || true
uv pip install -q -p .venv/bin/python --reinstall "opencv-contrib-python-headless==5.0.0.93"

say "ffmpeg"
if has ffmpeg && has ffprobe; then
  echo "  using system ffmpeg: $(command -v ffmpeg)"
else
  uv pip install -q -p .venv/bin/python "static-ffmpeg==3.0"
  eval "$(.venv/bin/static_ffmpeg_paths 2>/dev/null | grep -E '^(FFMPEG|FFPROBE)=')"
  chmod +x "$FFMPEG" "$FFPROBE"          # the wheel's zip loses the exec bit on some systems
  ln -sf "$FFMPEG" .venv/bin/ffmpeg; ln -sf "$FFPROBE" .venv/bin/ffprobe
  echo "  bundled ffmpeg installed into .venv/bin (no system install needed)"
fi
export PATH="$PWD/.venv/bin:$PATH"
ffmpeg -hide_banner -version | head -1

say "SyncNet weights"
M=vendor/syncnet/data/syncnet_v2.model
SHA=961e8696f888fce4f3f3a6c3d5b3267cf5b343100b238e79b2659bff2c605442
if [ ! -s "$M" ]; then
  echo "  downloading the SyncNet weights from the authors (VGG Oxford, 52 MB)"
  mkdir -p vendor/syncnet/data
  curl -L --fail -o "$M" https://www.robots.ox.ac.uk/~vgg/software/lipsync/data/syncnet_v2.model
fi
[ -s tests/example.avi ] || curl -sL --fail -o tests/example.avi https://www.robots.ox.ac.uk/~vgg/software/lipsync/data/example.avi || true
got=$(.venv/bin/python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$M")
[ "$got" = "$SHA" ] || { echo "SyncNet weights checksum mismatch ($got) — delete $M and re-run"; exit 1; }

uv cache clean -q 2>/dev/null || true                 # downloads are installed; free ~1 GB

say "Smoke test"
.venv/bin/python -c "import torch, cv2, mediapipe, python_speech_features, scipy; print('  packages OK, torch', torch.__version__)"
echo
echo "Installed. Prove it works:  bash tests/selftest.sh"
