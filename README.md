# lipsync-fix — when the voice doesn't match the lips

A skill for Claude that repairs videos where the sound is ahead of or behind the speaker's mouth,
then **proves** the repair by measuring again. Runs on your own computer; nothing is uploaded.

## How to use it (no technical knowledge needed)
1. Install **Claude Code** (https://claude.com/claude-code) on a Mac or Linux computer (Windows: inside WSL).
2. Download this folder (GitHub: green **Code** button → **Download ZIP**, then unzip).
3. Open Claude Code and say:
   > Here is a skill: `/path/to/lipsync-fix`. Read its SKILL.md and fix the lip-sync in `/path/to/my-video.mp4`.

Claude installs everything itself (3–10 min the first time, ~1.5 GB, no admin password), checks the install, measures
your video, fixes it, measures again, and gives you the fixed file plus a short before/after clip to listen to.

Optional — install it as a permanent skill so you never have to point to the folder:
copy the folder to `~/.claude/skills/lipsync-fix/`. Then just say "the lip-sync in my-video.mp4 is off, fix it".

## Use it yourself in a terminal
```bash
bash install.sh && bash tests/selftest.sh
.venv/bin/python scripts/lipsync_fix.py measure /abs/path/video.mp4
.venv/bin/python scripts/lipsync_fix.py fix /abs/path/video.mp4 --excerpt      # -> video_synced.mp4 + before/after clip
.venv/bin/python scripts/lipsync_fix.py fix /abs/path/video.mp4 --audio /abs/path/clean_voice.wav
```

What it can and can't do, how it was validated, licences: [`references/how-it-works.md`](references/how-it-works.md).
