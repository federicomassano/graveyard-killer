#!/bin/sh
# Starts gk-fetcher inside a stock python:3.12-slim container, with no image to build: ffmpeg,
# yt-dlp and gallery-dl are installed once into /config (a dataset) and reused at every start.
set -eu
CFG=/config
BIN=$CFG/bin
export PATH="$BIN:$CFG/venv/bin:$PATH" UV_TOOL_DIR="$CFG/uv-tools" UV_TOOL_BIN_DIR="$BIN" HOME="$CFG/home"
mkdir -p "$BIN" "$HOME"
if [ ! -x "$CFG/venv/bin/uv" ]; then
  python -m venv "$CFG/venv" && "$CFG/venv/bin/pip" install -q uv
fi
if [ ! -x "$BIN/ffmpeg" ] || [ ! -x "$BIN/ffprobe" ]; then
  python - <<'PY'
import os, tarfile, urllib.request
url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
path = "/tmp/ffmpeg.tar.xz"
urllib.request.urlretrieve(url, path)
with tarfile.open(path) as tar:
    for m in tar.getmembers():
        name = m.name.rsplit("/", 1)[-1]
        if m.isfile() and name in ("ffmpeg", "ffprobe"):
            m.name = name
            tar.extract(m, "/config/bin")
            os.chmod(f"/config/bin/{name}", 0o755)
PY
fi
for tool in yt-dlp gallery-dl; do
  [ -x "$BIN/$tool" ] || uv tool install "$tool"
done
exec python /app/server.py
