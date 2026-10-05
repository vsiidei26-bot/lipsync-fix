# How lipsync-fix works, and how we know it works

## The measurement
1. Video is resampled to 25 fps (short side ≤ 720 px, enough for a 224 px face). MediaPipe face detection finds the
   largest face in every frame; boxes are median-smoothed over 7 frames so the crop does not jitter.
2. Picture cuts are found with ffmpeg's scene score (> 0.30). **A chunk never straddles a cut**: two shots can carry
   two different syncs, and a chunk spanning both would average them away.
3. A square crop of 1.4× the face box is resized to 224×224 (what SyncNet was trained on). Stretches with a face for
   ≥ 2.5 s are split into equal chunks of about 8 s.
4. For each chunk SyncNet (VGG Oxford, 2016) compares 5-frame lip windows with 0.2 s of MFCC audio at every shift
   from −15 to +15 frames (±600 ms; widened to ±1.2 s automatically if the answer sits at the edge) and returns the best
   shift and a confidence.
5. Chunks with confidence < 3 are discarded: nobody visible is speaking (b-roll, screen recordings, a person on screen
   while someone else is heard). The report always says how many seconds could be checked.
6. Verdict:
   - **constant**: ≥ 75 % of confident chunks within one frame of the confidence-weighted median, spread ≤ 2 frames.
   - **drift**: a straight line over ≥ 20 s with slope ≥ 0.5 ms/s, every residual ≤ 1 frame **and every step between
     neighbouring chunks explained by the slope**. A montage glued with offsets that happen to grow is not drift: its
     steps jump by more than the slope allows, so it is classified as mixed.
   - **mixed**: runs of consecutive chunks with different offsets. Each boundary goes to the picture cut between the runs.
     If there is no cut (sync changes inside one continuous shot), the stretch is re-measured as one piece and SyncNet's
     per-frame distances give the frame t that minimises cost(A's shift before t) + cost(B's shift after t).

## The correction
For every part [a, b] with offset o: `new_audio(t) = old_audio(t + o)`. Parts are joined with 12 ms fades (no clicks).
- audio LATE (o > 0) → audio taken from later (the first o ms of the part are skipped)
- audio EARLY (o < 0) → silence/earlier audio in front (the part starts o ms later)
- drift → also `atempo = 1 + drift/1000` (pitch preserved)
- **Sound at the edges**: before rendering, the audio that would fall before the first frame or after the last one is
  measured (loudest 50 ms RMS). If it holds sound (> −42 dB), the first/last frame is frozen for that long so nothing is
  lost (video re-encoded with x264 CRF 16). Silence is simply dropped. `--extend never|always` overrides.
- Container and codec of the input are kept (`.mov` stays `.mov`, PCM stays PCM in mov/mkv); subtitle/data streams are
  carried when the container allows it, otherwise the report says they were left out.
- After rendering, the output is measured again **with chunks split at every part boundary**, so an error in a short
  part cannot hide inside a long average.

## Validation (2026-10-04)
| test | truth | measured | after fix |
|---|---|---|---|
| SyncNet authors' sample `example.avi` | 3 frames (authors' README) | −120 ms = 3 frames ✅ | — |
| same, audio delayed +300 ms / advanced −200 ms | base +300 / −200 | +160 / −320 (= base +280 / −200, within 1 frame) ✅ | 0 ms ✅ |
| real reel with a bad voiceover, 183 s, b-roll in the middle | — | −400 ms, 16/16 chunks | 0 ms, 16/16; video stream bit-identical (MD5) ✅ |
| drift: 0.4 % speed mismatch, 40 s | grows by 160 ms | +3.94 ms/s ✅ | 0 ms, 5/5 ✅ |
| mixed at a picture cut (20 s + 20 s, +240 / −200 on top of the clips' own sync) | cut at 20.02 s | boundary 20.02 s ✅ | ≤ 40 ms everywhere ✅ |
| mixed inside one continuous shot | change at 20.0 s | boundary 19.80 s (falls in a pause) ✅ | 0 ms ✅ |
| audio early 440 ms while speaking to the last frame | sound must survive | tail 440 ms −14 dB → frozen frame added ✅ | 0 ms ✅ |
| `tests/validate.sh` (drift, mixed, tail on the shipped sample) | — | 4 passed, 0 failed | ✅ |
| clean install + selftest on machines with nothing installed: macOS arm64 (no Homebrew/Python/ffmpeg) · Ubuntu 24.04 (only curl) · Debian + Python 3.11 | — | identical numbers everywhere ✅ | ✅ |

Resolution is one frame at 25 fps = 40 ms. Viewers notice about 45 ms of audio-early and 125 ms of audio-late
(ITU-R BT.1359), so a verified result is invisible.

## Limits (say these to the user, don't hide them)
- It only **moves** sound in time. If the mouth says different words, use a lip-sync generator instead.
- It needs a visible, talking face for at least 2.5 s somewhere in the clip.
- Several faces at once: the largest one is used. If the voice belongs to a smaller face, crop first.
- Very low light, heavy beards covering the lips, masks, or profile views lower the confidence. The script
  then says "cannot judge" rather than guessing.
- Parts of the video without a visible speaker are not measured; they move together with the neighbouring part.
- A separate voice file must be the SAME performance. A re-recorded take with different timing needs word-level
  re-timing or a lip-sync generator, not a shift.

## Licences — read before sharing
- Code in `vendor/syncnet/`: MIT (Joon Son Chung), licence file included.
- SyncNet weights `syncnet_v2.model`: published by the authors for download (robots.ox.ac.uk); **no separate licence
  file for the weights could be found**. The light version of this skill does not include them and downloads them
  from the authors on install. Prefer the light version for public sharing.
- `tests/example.avi`: the authors' demo clip, same situation as the weights (light version downloads it).

## Credits
- SyncNet: J. S. Chung, A. Zisserman, "Out of time: automated lip sync in the wild", ACCV 2016 workshops.
  Code: github.com/joonson/syncnet_python (MIT, see vendor/syncnet/LICENSE-syncnet.md). Weights distributed by the authors.
- MediaPipe face detection: Google (Apache-2.0).
