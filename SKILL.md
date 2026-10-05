---
name: lipsync-fix
description: Fix a video whose sound has slipped away from the lips — voice heard before or after the mouth moves, a voiceover that "doesn't match", lip-sync drifting over time, a montage where different parts are off by different amounts, or a separate / clean voice track of the same take that must be laid exactly onto the speaker's lips. Measures the offset with SyncNet, shifts (and if needed stretches, or fixes part by part) the audio, then re-measures to prove every part is within one frame. Use when the user says: bad lipsync, lip sync off, audio out of sync, voice ahead of / behind the lips, voiceover doesn't match the mouth, dubbed audio misaligned, AV offset, «ліпсинк поганий», «звук з'їхав», «войсовер не попадає в губи», «рассинхрон».
---

# lipsync-fix

Fixes one specific problem and proves the fix: **the sound and the lips are out of step.**
No generative model, nothing is uploaded anywhere. The picture is copied untouched, except in one case:
when sound would otherwise be lost at the very start or end, a frozen frame is added and the video is re-encoded once.

## ⚡ INSTRUCTIONS FOR CLAUDE — do exactly this, in this order

The user gave you this folder and a video. They may know nothing about Python or terminals. Do everything yourself.

1. **Find this folder** (the one containing this SKILL.md). Call it `SKILL_DIR`. Run every command below with
   `cd "$SKILL_DIR"` first. Never ask the user to type commands.
2. **Install, if `"$SKILL_DIR/.venv/bin/python"` does not exist yet:** `bash install.sh`
   - needs only `curl` and internet; installs Python 3.11, all packages and ffmpeg **inside this folder**, no admin password;
   - takes 3–10 min the first time and about 1.5 GB of disk; tell the user that before starting;
   - if it fails, read the last lines, fix the cause (disk space, network), run it again. It is safe to re-run.
3. **Prove the install:** `bash tests/selftest.sh` must end with `selftest OK`. If not, do not continue; report the output.
4. **Ask the user** (one short message, clickable options if you can): which video file (get the full path), is there a
   separate voice file, and what they notice (voice before the lips / after / worse over time / only some parts / not sure).
5. **Measure:** `.venv/bin/python scripts/lipsync_fix.py measure "/full/path/video.mp4"` → tell the user the result in one
   plain sentence, including how many seconds could be checked (Step 2 below).
6. **Fix:** `.venv/bin/python scripts/lipsync_fix.py fix "/full/path/video.mp4" --excerpt --json "/full/path/video_sync_report.json"`
7. **Only if the output contains `VERIFIED`**, deliver: the `_synced` file, the `_before_after.mp4` clip to listen to, and two lines
   about what was wrong and what was done (Step 4 below). Otherwise say exactly what the script printed. Never claim success without it.

## Where it runs

- ✅ **Claude Code, Codex, any agent with a terminal** on macOS or Linux (Windows: inside WSL/Ubuntu).
  Tested 2026-10-05 on: macOS arm64 with no Homebrew/Python/ffmpeg visible, clean Ubuntu 24.04 with only curl,
  Debian/Python 3.11 image. Same numbers everywhere.
- ❌ **claude.ai in the browser / Claude Desktop "uploaded skills"**: no local terminal, cannot run `install.sh`.
  Say so plainly; the user needs Claude Code (free to install) for this skill.
- Disk: about 1.5 GB (macOS) / 2 GB (Linux), once. Temp space while working: about 1 MB per second of video.

## When this skill is the right tool

| Symptom | What happens |
|---|---|
| Whole video: voice a bit before / after the mouth, same everywhere | `constant` → shift |
| Sync gets steadily worse towards the end | `drift` → shift + stretch (pitch kept) |
| A montage where parts are off by different amounts | `mixed` → each part fixed separately; boundaries snapped to the picture cut, or located frame-accurately if the change is inside one shot |
| A separate voice file **of the same take** (clean mic, re-export) must sit on the lips | `fix --audio voice.wav` |
| The mouth says different words from the audio (new script, other take, translation) | ❌ not a shift problem. Needs a lip-sync **generator** (LatentSync, Wav2Lip, Kling / Sync.so lip-sync). Tell the user honestly |
| No face on screen at all | ❌ "cannot judge": nothing is changed |

## Step 0 — install once (3–10 min)

```bash
bash install.sh          # only needs curl + internet; everything goes inside this folder (.tools/, .venv/)
bash tests/selftest.sh   # must end with "selftest OK" (~1 min)
bash tests/validate.sh   # optional, 5-10 min: drift, mixed montage, sound at the very end - "4 passed, 0 failed"
```
`bash install.sh` is idempotent: re-run it after any failure or if the folder was moved.
If `vendor/syncnet/data/syncnet_v2.model` is missing, it downloads it from the authors (VGG Oxford, 52 MB) and checks the sha256.

## Step 1 — ask, don't guess

One short message (clickable options if your interface has them):
1. Which file? Any separate voice file?
2. What do they notice: voice **before** the lips, **after**, **worse over time**, **only in some parts**, or "not sure"?
   Their answer is only a cross-check. The measurement decides.

## Step 2 — measure (changes nothing)

Always use **absolute paths** for files.
```bash
.venv/bin/python scripts/lipsync_fix.py measure "/abs/path/video.mp4" --json "/abs/path/report_before.json"
```
Say the result in plain words, including **how much of the video could be checked**. Example: *"The voice comes
400 ms before the lips. Measured on 120 s of 183 s; the other 63 s show no speaker (b-roll), so they can't be checked
and will simply move together with the rest."*
Exit codes: 0 measured · 2 cannot judge · 4 mixed · 1 error (bad file, ffmpeg failure).

## Step 3 — fix and verify

```bash
.venv/bin/python scripts/lipsync_fix.py fix "/abs/path/video.mp4" --excerpt --json "/abs/path/report.json"
# output: /abs/path/video_synced.mp4 (same container), plus video_synced_before_after.mp4 to listen to
# separate voice track of the same take:
.venv/bin/python scripts/lipsync_fix.py fix "/abs/path/video.mp4" --audio "/abs/path/voice.wav" --excerpt
```
The script: measures → plans (constant / drift / mixed parts) → checks whether any **sound** would fall outside the
picture (if yes, adds a frozen first/last frame so nothing is lost; `--extend never` to forbid) → renders →
**re-measures the result, with checks split at every part boundary** → prints `VERIFIED` only when every confident
chunk is within one frame (40 ms).
Exit codes: 0 fixed & verified (or already in sync) · 2 cannot judge / applied but cannot verify · 3 applied but NOT verified · 1 error.

## Step 4 — deliver honestly

In one or two lines, tell the user:
- what was wrong (direction, ms, which parts) and what was done (cut / delayed by N ms, stretched ×k, parts);
- that the re-measure says within one frame, **and on how many seconds** it could be checked;
- anything from the script's notes: frozen frame added (video re-encoded), extra audio tracks or subtitle streams not carried over;
- give them the `_before_after.mp4` (BEFORE, black flash, AFTER) so they can hear it themselves.

**Never say "fixed" without the script's `VERIFIED` line.** Exit code 3 → show the per-chunk table from the JSON and say what's still off.

## Notes for the agent

- Sign convention: `offset_ms > 0` = audio LATE, `< 0` = audio EARLY.
- Search window ±600 ms, widened automatically to ±1.2 s at the edge.
- Accuracy: one frame at 25 fps (40 ms). Viewers notice ≈ 45 ms audio-early and ≈ 125 ms audio-late (ITU-R BT.1359).
- How it works, full validation table, limits, credits → `references/how-it-works.md`.
