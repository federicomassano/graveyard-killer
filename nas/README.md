# gk-fetcher (personal fork only)

Moves the heavy part of a read to the NAS: `post.py read` runs there (yt-dlp, gallery-dl, ffmpeg),
the speech goes to Scriberr, and the computer gets the same JSON plus the frames. Not for the
upstream project: it breaks ADR 0001 (no server, no keys) on purpose, for one person's use.

```
computer: post.py read ──HTTPS, Bearer token──▶ gk-fetcher ──▶ post.py read ──▶ Scriberr (quick)
          ◀── JSON + frames (GET /files/<id>/<name>) ──
```

Nothing changes for the agent. If `GK_REMOTE_URL` is unset or the NAS does not answer (network
error, 401/403, 5xx), `post.py read` reads locally as before and adds `"remote": "unreachable, read locally"`.
`read --login` always stays on the computer (it uses its browser's cookies). A post the NAS read
but could not get (hidden, gone) is an answer, not a reason to read again from the computer's IP.

## Settings (computer)

Environment, or a JSON file: `GK_CONFIG`, else `~/.graveyard-killer/nas.json` (Claude Desktop starts
`post.py` without your shell's variables, so use the file; `chmod 600` it). Never put these in the Library.

```json
{"GK_REMOTE_URL": "https://gk.example.uk", "GK_REMOTE_TOKEN": "...",
 "GK_REMOTE_CF_ID": "...", "GK_REMOTE_CF_SECRET": "...",
 "GK_SCRIBERR_URL": "...", "GK_SCRIBERR_KEY": "...", "GK_SCRIBERR_PROFILE": "gk"}
```
`GK_REMOTE_CF_*` only with Cloudflare Access (service token). `GK_SCRIBERR_*` on the computer is only
useful to transcribe locally-read posts through Scriberr; the NAS has its own.

## Server (NAS)

`server.py` is stdlib only. Variables: `GK_TOKEN` (required), `GK_SCRIBERR_URL/KEY/PROFILE`, `GK_WORK`
(`/work`), `GK_POST`, `GK_TTL` (3600 s), `GK_MIN_GAP` (6 s between Instagram reads), `GK_PORT` (8080).
One read at a time. Only `instagram.com`, `instagr.am`, `tiktok.com`, `youtube.com`, `youtu.be` links,
and post.py still refuses anything that is not a post's own link. No cookies, no login.

Two ways to run it: `Dockerfile` (build once, `docker build -f nas/Dockerfile -t gk-fetcher:local .`
from the repo root) or `compose.truenas.yaml` + `bootstrap.sh`, which needs no image: copy
`server.py bootstrap.sh post.py link.py nas.py` into `pool_apps/config/gk-fetcher/app`.

## Reaching it from outside

A. Cloudflare: a public hostname `gk.<domain>` in the existing tunnel, service `http://<nas>:8096`, and an
Access application with two policies: *Allow* your Google account, *Service Auth* for a service token
(`GK_REMOTE_CF_ID/SECRET`). The bearer token still applies behind it.
B. Tailscale: `tailscale serve --bg --https=443 http://127.0.0.1:8096` on the NAS, then
`GK_REMOTE_URL=https://<nas>.<tailnet>.ts.net`. The computer must be on the tailnet.
