#!/usr/bin/env python3
"""lipsync-fix: measure how far the sound has slipped from the lips, put it back, prove it.

    python lipsync_fix.py measure VIDEO [--json R.json]
    python lipsync_fix.py fix     VIDEO [-o OUT] [--excerpt] [--json R.json]
    python lipsync_fix.py fix     VIDEO --audio VOICE.wav [-o OUT]   # separate voice of the SAME take -> onto the lips
    python lipsync_fix.py fix     VIDEO --offset-ms -400              # force a known shift (still re-measured)

Always pass absolute paths. Default output: next to the input, "<name>_synced.<same extension>".

Engine: SyncNet (Chung & Zisserman, VGG Oxford) on 25 fps face crops, 8-second chunks, +-600 ms search
(auto-widened to +-1.2 s at the edge). Chunks without a confident talking face are ignored.

Verdicts:  constant  one offset for the whole video            -> shift
           drift     offset changes steadily (speed mismatch)  -> shift + stretch (pitch kept)
           mixed     parts glued with different offsets        -> each part fixed separately,
                                                                  boundaries snapped to the nearest picture cut
           cannot_judge  no confident talking face anywhere    -> nothing changed
Sign convention everywhere: offset_ms > 0 = AUDIO LATE (heard after the lips), < 0 = AUDIO EARLY.

Exit codes: 0 ok (fixed & verified, or measured) · 1 error (bad input, ffmpeg failure)
            2 cannot judge / cannot verify · 3 fix applied but NOT verified · 4 mixed (measure mode only)
"""
import argparse, json, os, pathlib, re, shutil, subprocess, sys, tempfile, types, warnings

warnings.filterwarnings("ignore")
os.environ.setdefault("GLOG_minloglevel", "3")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
# bundled ffmpeg (installed into .venv/bin when the system has none) must be found first
os.environ["PATH"] = str(HERE.parent / ".venv" / "bin") + os.pathsep + os.environ.get("PATH", "")
SYNCNET_DIR = pathlib.Path(os.environ.get("SYNCNET_DIR", HERE.parent / "vendor" / "syncnet"))
FPS = 25
FRAME_MS = 1000 / FPS
MIN_CONF = 3.0
SPEECH_DB = -42.0          # max RMS above this in a lost piece of audio = there was sound worth keeping


class Fail(SystemExit):
    def __init__(self, msg):
        super().__init__(1); self.msg = msg


def run(cmd, stderr=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise Fail(f"ffmpeg/ffprobe failed: {' '.join(map(str, cmd))[:300]}\n{r.stderr[-1200:]}")
    return r.stderr if stderr else r.stdout


def probe(path):
    out = json.loads(run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=index,codec_type,codec_name,avg_frame_rate,duration:format=duration",
                          "-of", "json", str(path)]))
    vs = [s for s in out["streams"] if s["codec_type"] == "video"]
    aus = [s for s in out["streams"] if s["codec_type"] == "audio"]
    fps = None
    if vs:
        n, d = vs[0]["avg_frame_rate"].split("/"); fps = float(n) / float(d or 1) if float(d or 0) else None
    return {"duration": float(out["format"]["duration"]), "fps": fps, "video": bool(vs),
            "audio_tracks": len(aus), "audio_codec": aus[0]["codec_name"] if aus else None,
            "audio_duration": float(aus[0].get("duration") or out["format"]["duration"]) if aus else 0.0,
            "other_streams": len(out["streams"]) - len(vs[:1]) - len(aus[:1])}


def max_db(src, start, end):
    """Loudest 50 ms RMS (dB) of src audio in [start, end] - tells whether a piece we drop contained sound."""
    if end - start < 0.02:
        return -120.0
    o = run(["ffmpeg", "-v", "info", "-ss", f"{max(0, start):.3f}", "-to", f"{end:.3f}", "-i", str(src), "-vn",
             "-af", "astats=metadata=0:reset=0:length=0.05", "-f", "null", "-"], stderr=True)
    m = re.findall(r"RMS peak dB:\s*(-?[\d.]+|-inf)", o)
    vals = [float(x) for x in m if x != "-inf"]
    return max(vals) if vals else -120.0


# ------------------------------------------------------------------ measurement

def _face_boxes(frames):
    import cv2, mediapipe as mp
    boxes = []
    with mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.6) as fd:
        for f in frames:
            img = cv2.imread(str(f)); h, w = img.shape[:2]
            res = fd.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            if not res.detections:
                boxes.append(None); continue
            d = max(res.detections, key=lambda d: d.location_data.relative_bounding_box.width)
            b = d.location_data.relative_bounding_box
            if b.width < 0.10:
                boxes.append(None); continue
            boxes.append((b.xmin * w + b.width * w / 2, b.ymin * h + b.height * h / 2, max(b.width * w, b.height * h)))
    out = list(boxes)
    for i, b in enumerate(boxes):
        if b is not None:
            win = [x for x in boxes[max(0, i - 3): i + 4] if x is not None]
            out[i] = tuple(float(np.median([w[j] for w in win])) for j in range(3))
    return out


def _segments(boxes, min_len):
    segs, s = [], None
    for i, b in enumerate(boxes + [None]):
        if b is not None and s is None:
            s = i
        if b is None and s is not None:
            if i - s >= min_len:
                segs.append((s, i))
            s = None
    return segs


_NET = None


def _net():
    global _NET
    if _NET is None:
        model = SYNCNET_DIR / "data" / "syncnet_v2.model"
        if not model.exists():
            raise Fail(f"SyncNet weights not found at {model}. Run: bash install.sh")
        sys.path.insert(0, str(SYNCNET_DIR))
        import logging; logging.disable(logging.CRITICAL)
        from SyncNetInstance import SyncNetInstance
        _NET = SyncNetInstance(device="cpu")
        _NET.loadParameters(str(model))
    return _NET


def measure(video, audio=None, chunk_s=8.0, vshift=15, quiet=False, window=None, single=False, breaks=()):
    """window=(t0, t1): only measure inside this time range (used to locate a boundary precisely)."""
    import cv2
    net = _net()
    info = probe(video)
    cuts = sorted(set(scene_cuts(video, 0, info["duration"]) + [float(b) for b in breaks]))
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td); (td / "f").mkdir()
        # short side <= 720 px is plenty for a 224 px face crop and keeps temp space small
        sc = "scale='if(gt(iw,ih),-2,min(720,iw))':'if(gt(iw,ih),min(720,ih),-2)'"
        run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={FPS},{sc}", "-q:v", "3", str(td / "f" / "%06d.jpg")])
        run(["ffmpeg", "-v", "error", "-y", "-i", str(audio or video), "-vn", "-ac", "1", "-ar", "16000",
             "-acodec", "pcm_s16le", str(td / "full.wav")])
        frames = sorted((td / "f").glob("*.jpg"))
        boxes = _face_boxes(frames)
        for c in cuts:                       # a chunk must never straddle a picture cut (two shots = two syncs)
            k = int(round(c * FPS))
            if 0 < k < len(boxes):
                boxes[k] = None
        if window:
            lo, hi = int(window[0] * FPS), int(window[1] * FPS)
            boxes = [b if lo <= i < hi else None for i, b in enumerate(boxes)]
        minlen = int(min(2.5, chunk_s) * FPS)
        csize = int(chunk_s * FPS)
        chunks = []
        for s0, e0 in _segments(boxes, minlen):
            n = 1 if single else max(1, round((e0 - s0) / csize))          # equal chunks, none shorter than minlen
            step = (e0 - s0) / n
            chunks += [(int(s0 + i * step), int(s0 + (i + 1) * step)) for i in range(n)]
        rows = []
        for n, (cs, ce) in enumerate(chunks):
            cdir = td / f"c{n}"; cdir.mkdir()
            for j, i in enumerate(range(cs, ce)):
                img = cv2.imread(str(frames[i]))
                cx, cy, sz = boxes[i]; half = sz * 1.4 / 2; pad = int(half) + 2
                big = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(110, 110, 110))
                x0, y0 = int(cx - half + pad), int(cy - half + pad)
                cv2.imwrite(str(cdir / f"{j:06d}.jpg"),
                            cv2.resize(big[y0:y0 + int(2 * half), x0:x0 + int(2 * half)], (224, 224)))
            clip = td / f"c{n}.avi"
            run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", str(cdir / "%06d.jpg"),
                 "-ss", f"{cs / FPS:.3f}", "-t", f"{(ce - cs) / FPS:.3f}", "-i", str(td / "full.wav"),
                 "-c:v", "mjpeg", "-q:v", "2", "-c:a", "pcm_s16le", "-shortest", str(clip)])
            shutil.rmtree(cdir)
            opt = types.SimpleNamespace(tmp_dir=str(td / "sn"), reference=f"r{n}", batch_size=20, vshift=vshift)
            off, conf, dists = net.evaluate(opt, videofile=str(clip))
            clip.unlink(); shutil.rmtree(td / "sn" / f"r{n}", ignore_errors=True)
            # verified on synthetic shifts: audio delayed +300 ms -> SyncNet -7/-8 frames  => late_ms = -off*40
            row = {"t0": round(cs / FPS, 2), "t1": round(ce / FPS, 2),
                   "offset_ms": int(round(-int(off) * FRAME_MS)), "conf": round(float(conf), 2)}
            if single:
                row["_dists"] = np.asarray(dists)      # frames x (2*vshift+1), kept only for boundary search
            rows.append(row)
            if not quiet:
                mark = "" if row["conf"] >= MIN_CONF else "   (ignored: no confident talking face)"
                print(f"  {row['t0']:7.2f}-{row['t1']:7.2f}s  offset {row['offset_ms']:+5d} ms  conf {row['conf']:5.2f}{mark}",
                      flush=True)
    s = summarise(rows, info["duration"])
    if not single:
        for r in rows:
            r.pop("_dists", None)
    s["search_ms"] = int(vshift * FRAME_MS)
    s["picture_cuts"] = [round(c, 2) for c in cuts]
    return s


def _runs(good):
    """Consecutive confident chunks (by time) whose offsets stay within one frame of the run's median."""
    runs = []
    for r in sorted(good, key=lambda r: r["t0"]):
        if runs and abs(r["offset_ms"] - np.median([x["offset_ms"] for x in runs[-1]])) <= FRAME_MS * 1.01:
            runs[-1].append(r)
        else:
            runs.append([r])
    return [{"t0": rr[0]["t0"], "t1": rr[-1]["t1"], "chunks": len(rr),
             "offset_ms": int(np.median([x["offset_ms"] for x in rr]))} for rr in runs]


def summarise(rows, duration):
    good = [r for r in rows if r["conf"] >= MIN_CONF]
    s = {"chunks": rows, "confident_chunks": len(good), "total_chunks": len(rows), "duration_s": round(duration, 2),
         "verified_seconds": round(sum(r["t1"] - r["t0"] for r in good), 1)}
    if not good:
        s["verdict"] = "cannot_judge"; return s
    o = np.array([r["offset_ms"] for r in good], float); w = np.array([r["conf"] for r in good])
    t = np.array([(r["t0"] + r["t1"]) / 2 for r in good])
    order = np.argsort(o); cw = np.cumsum(w[order])
    s["offset_ms"] = int(o[order][np.searchsorted(cw, cw[-1] / 2)])     # confidence-weighted median
    s["spread_ms"] = int(np.ptp(o))
    s["agreeing_chunks"] = int((np.abs(o - s["offset_ms"]) <= FRAME_MS * 1.01).sum())
    runs = _runs(good)
    s["runs"] = runs
    if s["agreeing_chunks"] >= 0.75 * len(good) and s["spread_ms"] <= 2 * FRAME_MS:
        s["verdict"] = "constant"; return s
    # drift: straight line, small residual, AND every step between neighbours explained by the slope
    ts = np.argsort(t); t_s, o_s, w_s = t[ts], o[ts], w[ts]
    if len(good) >= 3 and np.ptp(t) >= 20:
        k, b = np.polyfit(t_s, o_s, 1, w=w_s)
        resid = o_s - (k * t_s + b)
        steps_ok = all(abs((o_s[i + 1] - o_s[i]) - k * (t_s[i + 1] - t_s[i])) <= FRAME_MS * 1.01 for i in range(len(t_s) - 1))
        if abs(k) >= 0.5 and np.max(np.abs(resid)) <= FRAME_MS * 1.01 and steps_ok:
            s.update(verdict="drift", drift_ms_per_s=round(float(k), 3), offset_at_0_ms=round(float(b), 1))
            return s
    s["verdict"] = "mixed" if len(runs) > 1 else "constant"
    return s


# ------------------------------------------------------------------ correction helpers

def scene_cuts(video, a, b):
    """Picture cuts (s) between a and b."""
    if b - a < 0.1:
        return []
    o = run(["ffmpeg", "-v", "info", "-ss", f"{max(0, a):.3f}", "-to", f"{b:.3f}", "-i", str(video), "-an",
             "-vf", "scale=320:-2,select='gt(scene,0.30)',showinfo", "-f", "null", "-"], stderr=True)
    return [max(0, a) + float(x) for x in re.findall(r"pts_time:([\d.]+)", o)]


def plan_segments(video, s, duration, audio=None):
    """[(start, end, offset_ms)] covering the whole video. Boundaries between runs with different offsets:
    the picture cut between them (chunks never straddle cuts, so a cut sits exactly there), or - for a
    sync change inside one continuous shot - located by re-measuring that stretch in 3 s pieces."""
    if s["verdict"] != "mixed":
        return [(0.0, duration, s["offset_ms"])]
    runs, cuts, bounds = s["runs"], s.get("picture_cuts", []), []
    for A, B in zip(runs, runs[1:]):
        between = [c for c in cuts if A["t1"] - 0.15 <= c <= B["t0"] + 0.15]
        if between:
            bounds.append(min(between, key=lambda c: abs(c - (A["t1"] + B["t0"]) / 2))); continue
        bounds.append(locate_boundary(video, audio, A, B, duration))
    edges = [0.0] + bounds + [duration]
    return [(edges[i], edges[i + 1], runs[i]["offset_ms"]) for i in range(len(runs))]


def locate_boundary(video, audio, A, B, duration):
    """Sync changes inside one continuous shot: frame-accurate split point.
    SyncNet gives a distance for every frame at every shift; the boundary is the frame t that minimises
    sum(dist at A's shift, frames < t) + sum(dist at B's shift, frames >= t)."""
    lo, hi = max(0.0, A["t1"] - 6.0), min(duration, B["t0"] + 6.0)
    fine = measure(video, audio, quiet=True, window=(lo, hi), single=True)
    best = None
    for r in fine["chunks"]:
        d = r.get("_dists")
        if d is None or d.ndim != 2 or len(d) < 10:
            continue
        vs = (d.shape[1] - 1) // 2
        ia = int(np.clip(vs + A["offset_ms"] / FRAME_MS, 0, d.shape[1] - 1))   # offset_ms = -off*40, idx = vs - off
        ib = int(np.clip(vs + B["offset_ms"] / FRAME_MS, 0, d.shape[1] - 1))
        ca, cb = np.cumsum(d[:, ia]), np.cumsum(d[::-1, ib])[::-1]
        cost = np.concatenate([[cb[0]], ca[:-1] + cb[1:], [ca[-1]]])
        k = int(np.argmin(cost))
        t = r["t0"] + k / FPS
        if best is None or len(d) > best[1]:
            best = (t, len(d))
    return best[0] if best else (A["t1"] + B["t0"]) / 2


def _atempo(f):
    parts = []
    while f > 2.0: parts.append("atempo=2.0"); f /= 2.0
    while f < 0.5: parts.append("atempo=0.5"); f /= 0.5
    parts.append(f"atempo={f:.6f}")
    return ",".join(parts)


def audio_graph(segments, duration, drift):
    """filter_complex on input 1 (audio source) producing [aout] of exactly `duration` seconds.
    new_audio(t) = old_audio(t + offset)  for t inside each segment."""
    n = len(segments)
    g = [f"[1:a:0]aresample=48000,asplit={n}" + "".join(f"[s{i}]" for i in range(n))] if n > 1 else \
        ["[1:a:0]aresample=48000[s0]"]
    for i, (a, b, off) in enumerate(segments):
        src_a, src_b = a + off / 1000, b + off / 1000
        f = [f"atrim=start={max(0, src_a):.4f}:end={max(0.001, src_b):.4f}", "asetpts=PTS-STARTPTS"]
        if src_a < 0:
            f.append(f"adelay={-src_a * 1000:.1f}:all=1")
        if drift and n == 1:
            f.append(_atempo(1.0 + drift / 1000.0))
        f.append(f"apad,atrim=end={b - a:.4f}")
        if n > 1:
            f.append(f"afade=t=in:d=0.012,afade=t=out:st={max(0, b - a - 0.012):.4f}:d=0.012")
        g.append(f"[s{i}]" + ",".join(f) + f"[p{i}]")
    g.append("".join(f"[p{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1,atrim=end={duration:.4f}[aout]")
    return ";".join(g)


def losses(src_audio, segments, info):
    """Sound that falls outside the picture after shifting: head of the first segment / tail of the last."""
    a0, b0, off0 = segments[0]
    aN, bN, offN = segments[-1]
    adur = info["audio_duration"]
    head = (0.0, off0 / 1000) if off0 > 0 else None                  # audio late -> its first N ms are dropped
    tail_start = bN + offN / 1000                                     # source time that maps to the video end
    tail = (tail_start, adur) if adur > tail_start + 0.02 else None
    out = {}
    for name, rng in (("head", head), ("tail", tail)):
        if rng:
            db = max_db(src_audio, *rng)
            out[name] = {"lost_s": round(rng[1] - rng[0], 3), "max_db": round(db, 1), "had_sound": db > SPEECH_DB}
    return out


def render(video, audio_src, out, segments, drift, extend):
    info = probe(video)
    dur = info["duration"]
    head_pad = extend.get("head", 0.0); tail_pad = extend.get("tail", 0.0)
    segs = segments
    if head_pad or tail_pad:      # frozen first/last frame gives the lost sound a picture to sit on
        segs = [(a + head_pad if i else a, b + head_pad, o) for i, (a, b, o) in enumerate(segments)]
        segs = [(0.0, segs[0][1], segments[0][2] - head_pad * 1000)] + segs[1:]
        segs[-1] = (segs[-1][0], dur + head_pad + tail_pad, segs[-1][2])
        dur = dur + head_pad + tail_pad
    graph = audio_graph(segs, dur, drift)
    ext = out.suffix.lower()
    acodec = ["-c:a", "pcm_s16le"] if (ext in (".mov", ".mkv") and (info["audio_codec"] or "").startswith("pcm")) \
        else ["-c:a", "aac", "-b:a", "192k"]
    mp4ish = ext in (".mp4", ".m4v", ".mov")
    if head_pad or tail_pad:
        vf = f"tpad=start_mode=clone:start_duration={head_pad:.3f}:stop_mode=clone:stop_duration={tail_pad:.3f}"
        vmap = ["-filter_complex", f"[0:v:0]{vf}[vout];{graph}", "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p"]
    else:
        vmap = ["-filter_complex", graph, "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy"]
    base = ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-i", str(audio_src)]
    tail_opts = acodec + ["-map_metadata", "0"] + (["-movflags", "+faststart"] if mp4ish else []) + [str(out)]
    keep_extra = [] if (head_pad or tail_pad) else ["-map", "0:s?", "-map", "0:d?", "-c:s", "copy"]
    try:
        run(base + vmap + keep_extra + tail_opts)
        dropped = False
    except Fail:
        run(base + vmap + tail_opts)          # container refused subtitle/data streams -> without them
        dropped = True
    return {"reencoded_video": bool(head_pad or tail_pad), "dropped_extra_streams": dropped}


def excerpt(before, after, out, s):
    """10 s before, 0.4 s black, the same 10 s after - so a human can hear the difference."""
    good = [r for r in s.get("chunks", []) if r["conf"] >= MIN_CONF]
    if not good:
        return None
    r = max(good, key=lambda r: r["conf"])
    st = r["t0"]; ln = min(10.0, r["t1"] - r["t0"] + 2)
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{st:.2f}", "-t", f"{ln:.2f}", "-i", str(before),
         "-ss", f"{st:.2f}", "-t", f"{ln:.2f}", "-i", str(after),
         "-filter_complex",
         "[0:v]scale=720:-2,setsar=1,fps=30[v0];[1:v]scale=720:-2,setsar=1,fps=30[v1];"
         "[v0]split[v0a][v0b];[v0b]trim=0:0.4,setpts=PTS-STARTPTS,drawbox=c=black:t=fill[gap];"
         "[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
         "aevalsrc=0|0:d=0.4:s=48000,aformat=channel_layouts=stereo[ga];"
         "[v0a][a0][gap][ga][v1][a1]concat=n=3:v=1:a=1[v][a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k", str(out)])
    return {"file": str(out), "from_s": st, "length_s": ln, "note": "first part = BEFORE, black flash, second = AFTER"}


# ------------------------------------------------------------------ CLI

def explain(s, label):
    v = s["verdict"]
    if v == "cannot_judge":
        print(f"{label}: no confident talking face in any chunk - cannot judge."); return
    cov = f"measured on {s['verified_seconds']:.0f} s of {s['duration_s']:.0f} s (the rest has no visible speaker)"
    if v == "mixed":
        parts = "; ".join(f"{r['t0']:.0f}-{r['t1']:.0f}s {r['offset_ms']:+d} ms" for r in s["runs"])
        print(f"{label}: MIXED offsets in different parts: {parts}. {cov}"); return
    off = s["offset_ms"] if v == "constant" else s["offset_at_0_ms"]
    side = "LATE (heard after the lips)" if off > 0 else "EARLY (heard before the lips)" if off < 0 else "in sync"
    extra = f"; drifting {s['drift_ms_per_s']:+.2f} ms per second" if v == "drift" else ""
    print(f"{label}: audio {abs(off):.0f} ms {side}{extra}; {s['agreeing_chunks']}/{s['confident_chunks']} confident chunks agree; {cov}")


def measure_widen(video, audio=None, quiet=False, breaks=()):
    s = measure(video, audio, quiet=quiet, breaks=breaks)
    if s.get("offset_ms") is not None and abs(s["offset_ms"]) >= 15 * FRAME_MS - 1:
        if not quiet:
            print("offset is at the edge of the +-600 ms search - widening to +-1.2 s ...")
        s = measure(video, audio, vshift=30, quiet=quiet, breaks=breaks)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["measure", "fix"])
    ap.add_argument("video")
    ap.add_argument("-o", "--out")
    ap.add_argument("--audio", help="separate voice track OF THE SAME TAKE to lay onto the lips")
    ap.add_argument("--offset-ms", type=float, help="force this shift instead of the measured one")
    ap.add_argument("--no-drift", action="store_true", help="never stretch")
    ap.add_argument("--extend", choices=["auto", "never", "always"], default="auto",
                    help="freeze first/last frame when the shift would push SOUND outside the picture (re-encodes video)")
    ap.add_argument("--excerpt", action="store_true", help="also write a 10 s before/after clip to listen to")
    ap.add_argument("--json", help="write the full report here")
    a = ap.parse_args()

    video = pathlib.Path(a.video).expanduser().resolve()
    if not video.exists():
        raise Fail(f"no such file: {video}")
    info = probe(video)
    if not info["video"]:
        raise Fail("this file has no picture - nothing to sync against")
    if not info["audio_tracks"] and not a.audio:
        raise Fail("video has no audio track; pass --audio VOICE.wav")
    audio_src = pathlib.Path(a.audio).expanduser().resolve() if a.audio else video
    if a.audio:
        info["audio_duration"] = probe(audio_src)["duration"]
        info["audio_codec"] = probe(audio_src)["audio_codec"]
    print("measuring", video.name, "...")
    before = measure_widen(video, a.audio)
    explain(before, "BEFORE")
    report = {"video": str(video), "before": before}
    code = 0

    if a.mode == "measure":
        code = {"cannot_judge": 2, "mixed": 4}.get(before["verdict"], 0)
    else:
        forced = a.offset_ms is not None
        if not forced and before["verdict"] == "cannot_judge":
            print("Nothing changed."); code = 2
        else:
            drift = 0.0
            if forced:
                segments = [(0.0, info["duration"], float(a.offset_ms))]
            else:
                if before["verdict"] == "drift" and not a.no_drift:
                    drift = before["drift_ms_per_s"]
                    segments = [(0.0, info["duration"], before["offset_at_0_ms"])]
                else:
                    segments = plan_segments(video, before, info["duration"], a.audio)
            nothing = (not forced and not a.audio and not drift and len(segments) == 1
                       and abs(segments[0][2]) < FRAME_MS / 2)
            if nothing:
                print("Already in sync (under half a frame). Nothing changed.")
                report["result"] = "in_sync"
            else:
                lost = losses(audio_src, segments, info)
                extend = {}
                for k, v in lost.items():
                    want = a.extend == "always" or (a.extend == "auto" and v["had_sound"])
                    if want:
                        extend[k] = v["lost_s"]
                    tag = "kept: picture extended with a frozen frame" if want else (
                        "dropped (silence)" if not v["had_sound"] else "DROPPED - contains sound (use --extend auto)")
                    print(f"{k} of the audio: {v['lost_s']*1000:.0f} ms falls outside the picture, loudest {v['max_db']} dB -> {tag}")
                report["edge_audio"] = lost; report["extended"] = extend
                out = pathlib.Path(a.out).expanduser().resolve() if a.out else video.with_name(video.stem + "_synced" + video.suffix)
                if len(segments) > 1:
                    print("fixing part by part:", "; ".join(f"{s0:.2f}-{s1:.2f}s {o:+.0f} ms" for s0, s1, o in segments))
                rinfo = render(video, audio_src, out, segments, drift, extend)
                report.update(rinfo, segments=segments, drift_ms_per_s=drift, output=str(out))
                if rinfo["dropped_extra_streams"]:
                    print("note: subtitle/data streams could not go into this container and were left out")
                if info["audio_tracks"] > 1 and not a.audio:
                    print(f"note: the input had {info['audio_tracks']} audio tracks; the output has the first one, synced")
                desc = "; ".join(f"{'cut' if o > 0 else 'delayed'} audio {abs(o):.0f} ms" + (f" ({s0:.0f}-{s1:.0f}s)" if len(segments) > 1 else "")
                                 for s0, s1, o in segments)
                print(f"applied: {desc}" + (f", stretched x{1 + drift / 1000:.5f}" if drift else "") + f" -> {out}")
                print("re-measuring the result ...")
                shift_in = extend.get("head", 0.0)
                after = measure_widen(out, quiet=True, breaks=[b + shift_in for _, b, _ in segments[:-1]])
                explain(after, "AFTER")
                report["after"] = after
                if after["verdict"] == "cannot_judge":
                    report["result"] = "applied_cannot_verify"; code = 2
                    print("CANNOT VERIFY: no confident talking face to check the result against.")
                else:
                    resid = [r["offset_ms"] for r in after["chunks"] if r["conf"] >= MIN_CONF]
                    ok = max(abs(x) for x in resid) <= FRAME_MS
                    report["result"] = "fixed_verified" if ok else "not_verified"
                    print("VERIFIED: every confident chunk is within one frame (40 ms)." if ok else
                          f"NOT VERIFIED: residual up to {max(abs(x) for x in resid)} ms - see the per-chunk report.")
                    code = 0 if ok else 3
                if a.excerpt:
                    ex = excerpt(video, out, out.with_name(out.stem + "_before_after.mp4"), after if after["verdict"] != "cannot_judge" else before)
                    report["excerpt"] = ex
                    if ex:
                        print("listen:", ex["file"], "(BEFORE, black flash, AFTER)")
    if a.json:
        pathlib.Path(a.json).expanduser().write_text(json.dumps(report, indent=1, default=float))
    sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except Fail as e:
        print("ERROR:", e.msg, file=sys.stderr); sys.exit(1)
