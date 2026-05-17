"""
Valorant Highlight Generator
- Medal.tv recordings: instant load from database (exact kills)
- OBS / any recorder: OCR-based kill detection — reads your name from the
  kill feed, filters out deaths, ignores other players. Fully automatic.
"""

import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import subprocess
import sqlite3
import re
import os
import glob
import tempfile
from pathlib import Path

FFMPEG  = r"C:\ffmpeg\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg\bin\ffprobe.exe"

# Kill-feed crop in 1920x1080 source
KF_CW, KF_CH, KF_CX, KF_CY = 620, 200, 1300, 25
# Killer name appears at cx < 380; victim name at cx > 380
KILL_SIDE_THRESHOLD = 380

# Score hexagon positions (1920x1080): left team score and right team score
# These never reflect game-world content — HUD overlay only
SC_W, SC_H = 80, 50
SC_LX, SC_RX, SC_Y = 755, 1090, 5

ROUND_GAP  = 40   # Valorant buy phase = 30s; inter-round kill gap is always ≥ ~35s
STREAK_GAP = 300

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ─────────────────────────────────────────────────────────────
# OCR — lazy-loaded so startup stays instant
# ─────────────────────────────────────────────────────────────

_ocr_reader = None

def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        try:
            _ocr_reader = easyocr.Reader(['en'], gpu=True, verbose=False)
        except Exception:
            _ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
    return _ocr_reader


def classify_frame(frame_path, player_name, min_conf=0.25):
    """
    Returns 'kill', 'death', or 'other' for a kill-feed crop frame.
    Killer name appears on the LEFT side of the bar (cx < 380).
    Victim name appears on the RIGHT side (cx > 380).
    min_conf: lower this (e.g. 0.15) for supplemental/fallback scans.
    """
    reader   = get_ocr_reader()
    results  = reader.readtext(frame_path, detail=1)
    name_key = player_name.lower().replace(' ', '')

    for bbox, text, conf in results:
        if conf < min_conf:
            continue
        text_key = text.lower().replace(' ', '')
        # Accept exact match or partial (≥4 chars) in either direction
        matched = (name_key in text_key or
                   (text_key in name_key and len(text_key) >= 4))
        if not matched:
            continue
        cx = sum(p[0] for p in bbox) / 4
        return 'kill' if cx < KILL_SIDE_THRESHOLD else 'death'

    return 'other'


# ─────────────────────────────────────────────────────────────
# Medal.tv database
# ─────────────────────────────────────────────────────────────

def _find_medal_db():
    pattern = os.path.join(os.path.expanduser("~"),
                           "AppData", "Roaming", "Medal", "medal-*.db")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def _kills_from_medal(video_path, db_path):
    """Return all kill timestamps stored in the Medal.tv DB for this video (any player)."""
    if not db_path or not os.path.exists(db_path):
        return []
    filename = os.path.basename(video_path).encode()
    try:
        conn = sqlite3.connect(db_path)
        conn.text_factory = bytes
        cur  = conn.cursor()
        cur.execute("SELECT video_path, metadata FROM contents ORDER BY created_at DESC")
        kills = []
        for vp, meta in cur.fetchall():
            if filename not in (vp or b""):
                continue
            raw = (meta or b"").decode("utf-8", errors="replace")
            for m in re.finditer(r"([\d]+\.[\d]+)GnameGKill", raw):
                kills.append(float(m.group(1)))
        conn.close()
        # Dedup kills that are within 0.5s of each other (same kill logged twice)
        kills.sort()
        deduped = []
        for k in kills:
            if not deduped or k - deduped[-1] > 0.5:
                deduped.append(k)
        return deduped
    except Exception:
        return []


def find_boundaries_from_all_kills(all_kills, min_gap=33):
    """
    Find round boundaries from gaps in the ALL-players kill timeline.
    During the buy phase (~30 s) no kills happen anywhere in the game.
    Using every player's kills gives a dense timeline — a 33s+ gap can only
    mean a buy phase, not a lull within a round.

    Returns list of boundary timestamps placed 2s after the last kill of each round.
    """
    if len(all_kills) < 2:
        return []
    kills = sorted(all_kills)
    boundaries = []
    for i in range(1, len(kills)):
        gap = kills[i] - kills[i - 1]
        if gap >= min_gap:
            boundaries.append(kills[i - 1] + 2.0)   # 2s after last kill = round over
    return boundaries


def build_round_kill_dict(all_kills, player_kills, boundaries):
    """
    Returns a dict:  round_num → {'yours': [...], 'others': [...], 'total': int}
    Uses boundaries to assign kills to rounds.
    """
    starts = [0.0] + sorted(boundaries)
    def round_for(t):
        rn = 1
        for i, s in enumerate(starts):
            if t >= s:
                rn = i + 1
        return rn

    player_set = set(round(t, 1) for t in player_kills)
    rd = {}
    for t in all_kills:
        rn = round_for(t)
        rd.setdefault(rn, {'yours': [], 'others': []})
        if round(t, 1) in player_set:
            rd[rn]['yours'].append(t)
        else:
            rd[rn]['others'].append(t)
    for rn in rd:
        rd[rn]['total'] = len(rd[rn]['yours']) + len(rd[rn]['others'])
    return rd


def verify_medal_kills(video_path, medal_kills, player_name, progress_cb=None):
    """
    OCR-verify each Medal.tv timestamp — keeps only timestamps where
    player_name appears on the KILLER side (cx < KILL_SIDE_THRESHOLD).
    Medal stores kills from all players; this filters to only your own.
    """
    verified = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, t in enumerate(medal_kills):
            if progress_cb:
                progress_cb(i / max(len(medal_kills), 1))

            # Kill feed persists ~5s after the kill event.
            # Try from t+0 through t+5, plus t-1 as fallback.
            # Only stop early on 'kill' — a 'death' at one offset doesn't
            # rule out finding the correct kill entry at another offset.
            classification = 'other'
            for offset in (0, 1, 2, 3, 4, 5, -1):
                seek_to  = max(0, round(t) + offset)
                pre_seek = max(0, seek_to - 3)
                frame_path = os.path.join(tmpdir, f"medal_{i}_{offset}.jpg")
                subprocess.run(
                    [FFMPEG, "-y",
                     "-ss", str(pre_seek), "-i", video_path,
                     "-ss", str(seek_to - pre_seek), "-vframes", "1",
                     "-vf", f"crop={KF_CW}:{KF_CH}:{KF_CX}:{KF_CY}",
                     "-q:v", "2", frame_path],
                    capture_output=True)
                if not os.path.exists(frame_path):
                    continue
                result = classify_frame(frame_path, player_name)
                if result == 'kill':
                    classification = 'kill'
                    break  # found it — stop
                if result == 'death' and classification == 'other':
                    classification = 'death'  # keep trying — may find kill later

            if classification == 'kill':
                verified.append(t)

    return verified


# ─────────────────────────────────────────────────────────────
# Video scanning
# ─────────────────────────────────────────────────────────────

def _video_duration(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet",
                            "-show_entries", "format=duration",
                            "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0


def _pixel_scan(video_path, progress_cb=None):
    """Fast 2fps scan — finds frames where kill feed is active (all players)."""
    FRAME_SIZE = KF_CW * KF_CH * 3

    def detect(buf):
        for row in range(KF_CH):
            g = r = 0
            for x in range(0, KF_CW, 3):
                idx = (row * KF_CW + x) * 3
                if idx + 2 >= len(buf): break
                rv, gv, bv = buf[idx], buf[idx+1], buf[idx+2]
                # Teal/cyan killer name: G dominant over R by 30+, G high.
                # Do NOT require G > B — Valorant teal has G ≈ B (e.g. G=200 B=195).
                if gv > 130 and gv > rv + 30: g += 1
                # Red victim name: R dominant over both G and B
                if rv > 150 and rv > gv + 35 and rv > bv + 35: r += 1
            if g >= 6 and r >= 5:
                return True
        return False

    dur = _video_duration(video_path)
    cmd = [FFMPEG, "-i", video_path,
           "-vf", f"crop={KF_CW}:{KF_CH}:{KF_CX}:{KF_CY},fps=2",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1", "-an"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    raw, frame_idx, buf = [], 0, b""
    while True:
        chunk = proc.stdout.read(FRAME_SIZE - len(buf))
        if not chunk: break
        buf += chunk
        if len(buf) < FRAME_SIZE: continue
        frame, buf = buf[:FRAME_SIZE], buf[FRAME_SIZE:]
        t = frame_idx / 2.0
        if detect(frame): raw.append(t)
        if progress_cb and dur > 0 and frame_idx % 10 == 0:
            progress_cb(t / dur)
        frame_idx += 1
    proc.wait()

    events, prev = [], None
    for h in raw:
        if prev is None or h - prev > 2:
            events.append(h)
        prev = h
    return events


def scan_with_ocr(video_path, player_name, progress_cb=None):
    """
    Full pipeline:
      1. Fast pixel scan → candidate kill-feed timestamps
      2. Extract one frame per candidate
      3. OCR classify → kill / death / other
    Returns (kills, deaths) as lists of timestamps.
    """
    # Stage 1: pixel scan (reports 0-50% progress)
    def stage1_cb(p):
        if progress_cb: progress_cb(p * 0.5)

    events = _pixel_scan(video_path, progress_cb=stage1_cb)

    # Stage 2: OCR on each candidate frame (reports 50-100% progress)
    kills, deaths = [], []

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, t in enumerate(events):
            if progress_cb:
                progress_cb(0.5 + 0.5 * i / max(len(events), 1))

            frame_path = os.path.join(tmpdir, f"kf_{i}.jpg")
            seek_base  = max(0, int(t) - 5)
            subprocess.run(
                [FFMPEG, "-y",
                 "-ss", str(seek_base), "-i", video_path,
                 "-ss", str(int(t) - seek_base), "-vframes", "1",
                 "-vf", f"crop={KF_CW}:{KF_CH}:{KF_CX}:{KF_CY}",
                 "-q:v", "2", frame_path],
                capture_output=True)

            if not os.path.exists(frame_path):
                continue

            result = classify_frame(frame_path, player_name)
            if result == 'kill':
                kills.append(t)
            elif result == 'death':
                deaths.append(t)

    return kills, deaths


def supplement_with_pixel_scan(video_path, known_kills, player_name, progress_cb=None):
    """
    Pixel-scan the full video for kill-feed activity, then OCR-verify any hit
    that is NOT already covered by a known kill (within ±6 s).
    Tries 5 offsets per candidate + lower OCR confidence to catch borderline frames.
    Returns a list of EXTRA kill timestamps the DB missed.
    """
    def scan_cb(p):
        if progress_cb: progress_cb(p * 0.5)

    candidates = _pixel_scan(video_path, progress_cb=scan_cb)

    # Filter: keep only candidates not covered by an existing kill
    known_sorted = sorted(known_kills)
    uncovered = []
    for t in candidates:
        if not any(abs(t - k) <= 6 for k in known_sorted):
            uncovered.append(t)

    if not uncovered:
        return []

    extra_kills = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, t in enumerate(uncovered):
            if progress_cb:
                progress_cb(0.5 + 0.5 * i / max(len(uncovered), 1))

            classification = 'other'
            # Try multiple offsets — kill feed persists ~5s after the event
            for offset in (0, 1, 2, 3, -1):
                seek_to   = max(0, round(t) + offset)
                pre_seek  = max(0, seek_to - 3)
                frame_path = os.path.join(tmpdir, f"sup_{i}_{offset}.jpg")
                subprocess.run(
                    [FFMPEG, "-y",
                     "-ss", str(pre_seek), "-i", video_path,
                     "-ss", str(seek_to - pre_seek), "-vframes", "1",
                     "-vf", f"crop={KF_CW}:{KF_CH}:{KF_CX}:{KF_CY}",
                     "-q:v", "2", frame_path],
                    capture_output=True)

                if not os.path.exists(frame_path):
                    continue

                # Use lower confidence (0.15) for supplemental — borderline frames
                result = classify_frame(frame_path, player_name, min_conf=0.15)
                if result == 'kill':
                    classification = 'kill'
                    break

            if classification == 'kill':
                extra_kills.append(t)

    return extra_kills


# ─────────────────────────────────────────────────────────────
# Round grouping / clip cutting
# ─────────────────────────────────────────────────────────────

def parse_timestamps(text):
    kills = []
    for token in re.split(r"[,\s\n]+", text.strip()):
        token = token.strip()
        if not token: continue
        try:
            if ":" in token:
                p = token.split(":")
                # Supports both MM:SS and MM:SS.ss
                kills.append(int(p[0]) * 60 + float(p[1]))
            else:
                kills.append(float(token))
        except ValueError:
            pass
    kills.sort()
    out = []
    for k in kills:
        # Dedup only kills within 0.4s — preserves rapid double-kills
        if not out or k - out[-1] > 0.4:
            out.append(k)
    return out


def group_rounds(kills, gap=ROUND_GAP):
    if not kills: return []
    rounds, cur = [], [kills[0]]
    for k in kills[1:]:
        if k - cur[-1] > gap:
            rounds.append(cur); cur = [k]
        else:
            cur.append(k)
    rounds.append(cur)
    return rounds



def best_streak(rounds, min_r=3, gap=STREAK_GAP):
    if not rounds: return rounds
    best, cur = [], [rounds[0]]
    for i in range(1, len(rounds)):
        if rounds[i][0] - rounds[i-1][-1] < gap:
            cur.append(rounds[i])
        else:
            if len(cur) > len(best): best = cur
            cur = [rounds[i]]
    if len(cur) > len(best): best = cur
    return best if len(best) >= min_r else rounds


def scan_round_boundaries(video_path, progress_cb=None):
    """
    Detect round boundaries by finding the round-end banner screen.
    After every Valorant round, a large bright-white banner appears in the center:
    THRIFTY / FLAWLESS / ACE / CLUTCH / VICTORY / DEFEAT etc.
    This is far more reliable than score-digit tracking — the banners are opaque,
    large, and appear every single round without exception.

    Detection: count bright-white pixels in the center band (x=560,y=70,w=800,h=220).
    During a round-end banner: 300+ white samples (every-4th-pixel grid).
    During normal gameplay: < 100 white samples (rare bright patches in game world).
    """
    # Center band containing THRIFTY / ACE / CLUTCH text (1920×1080 source)
    BW, BH, BX, BY = 800, 220, 560, 70
    FRAME_SIZE = BW * BH * 3
    FPS        = 2          # 2 fps is enough — banners last 3-5 s
    WHITE_MIN  = 700        # THRIFTY/ACE banner gives ~1000 samples; noise gives <400
    MIN_GAP    = 30         # shortest possible Valorant round (seconds)

    dur = _video_duration(video_path)
    if dur == 0:
        return []

    cmd = [FFMPEG, "-i", video_path,
           "-vf", f"crop={BW}:{BH}:{BX}:{BY},fps={FPS}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1", "-an"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    frame_idx = 0
    buf       = b""
    hits      = []   # timestamps where banner was detected

    while True:
        chunk = proc.stdout.read(FRAME_SIZE - len(buf))
        if not chunk:
            break
        buf += chunk
        if len(buf) < FRAME_SIZE:
            continue
        frame, buf = buf[:FRAME_SIZE], buf[FRAME_SIZE:]

        t = frame_idx / FPS

        # Count bright-white pixels (sample every 4th pixel in both axes)
        white = 0
        for row in range(0, BH, 4):
            for col in range(0, BW, 4):
                idx = (row * BW + col) * 3
                if idx + 2 >= len(frame):
                    break
                r, g, b = frame[idx], frame[idx+1], frame[idx+2]
                # White: all channels very bright AND roughly equal
                if r > 180 and g > 180 and b > 180 and abs(int(r)-int(g)) < 40:
                    white += 1

        if white >= WHITE_MIN:
            hits.append(t)

        if progress_cb and dur > 0 and frame_idx % 20 == 0:
            progress_cb(t / dur)
        frame_idx += 1

    proc.wait()

    if not hits:
        return []

    # Group consecutive detections (same banner) into single events
    groups, cur = [], [hits[0]]
    for h in hits[1:]:
        if h - cur[-1] <= 6:   # same banner lasts up to ~5 s
            cur.append(h)
        else:
            groups.append(cur)
            cur = [h]
    groups.append(cur)

    # Boundary = first frame of each banner group (= round just ended)
    boundaries = []
    last_b = -MIN_GAP
    for g in groups:
        b = g[0]
        if b - last_b >= MIN_GAP:
            boundaries.append(b)
            last_b = b

    # Sanity: a full competitive match produces 12-24 banners; multi-match = more.
    # Fewer than 10 → video is too short or detection failed → fall back.
    if len(boundaries) < 10:
        return []

    return boundaries


def assign_kills_to_rounds(kills, boundaries, gap=None):
    """
    Assign each kill to a round number based on score-change boundaries.
    After the initial assignment, re-split any round whose kills span more than
    `gap` seconds — this catches rounds where the boundary scanner missed a
    transition (e.g. Round 7 with kills spanning 13 minutes = 3+ missed rounds).
    """
    if not kills:
        return []
    if gap is None:
        gap = ROUND_GAP

    starts = [0.0] + sorted(boundaries)

    round_dict = {}
    for kill_t in kills:
        rnum = 1
        for i, s in enumerate(starts):
            if kill_t >= s:
                rnum = i + 1
        round_dict.setdefault(rnum, []).append(kill_t)

    # Re-split rounds where consecutive kills are > gap apart (missed boundary)
    result = []
    for rn, ts in sorted(round_dict.items()):
        ts_sorted = sorted(ts)
        groups = []
        cur = [ts_sorted[0]]
        for kt in ts_sorted[1:]:
            if kt - cur[-1] > gap:
                groups.append(cur)
                cur = [kt]
            else:
                cur.append(kt)
        groups.append(cur)

        letters = "bcdefghij"
        for gi, g in enumerate(groups):
            label = rn if gi == 0 else f"{rn}{letters[min(gi-1, len(letters)-1)]}"
            # Cap at 5 kills per label
            while len(g) > 5:
                result.append((label, g[:5]))
                g = g[5:]
                label = f"{label}x"
            result.append((label, g))

    return result


# ─────────────────────────────────────────────────────────────
# GPU encoder detection (NVENC)
# ─────────────────────────────────────────────────────────────

_nvenc_available = None   # None = not yet tested

def _check_nvenc():
    """Return True if h264_nvenc is available on this machine."""
    global _nvenc_available
    if _nvenc_available is not None:
        return _nvenc_available
    try:
        r = subprocess.run(
            [FFMPEG, "-f", "lavfi", "-i", "nullsrc=s=128x128:d=1",
             "-t", "1", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=10)
        _nvenc_available = (r.returncode == 0)
    except Exception:
        _nvenc_available = False
    return _nvenc_available


def _video_enc_args():
    """
    Return the best available video encoder args as a flat list.
    NVENC (GPU):  h264_nvenc  -preset p4  -rc vbr  -cq 18
    CPU fallback: libx264     -preset slow -crf 16
    NVENC is ~10x faster and produces equal or better quality.
    CQ 18 ≈ CRF 16 visually — both are near-transparent quality.
    """
    if _check_nvenc():
        return ["-c:v", "h264_nvenc",
                "-preset", "p4",      # p1=fastest … p7=best; p4 = balanced
                "-rc", "vbr",         # variable bitrate → better quality
                "-cq", "18",          # quality target (lower = better)
                "-b:v", "0"]          # let CQ drive bitrate fully
    else:
        return ["-c:v", "libx264",
                "-preset", "slow",    # better compression than 'fast'
                "-crf", "16"]         # slightly higher quality than old crf 18


def cut_clip(video, ss, duration, output):
    """Cut a single clip — uses GPU (NVENC) if available, else CPU x264."""
    subprocess.run(
        [FFMPEG, "-y",
         "-ss", str(max(0, ss)), "-i", video,
         "-t", str(duration)]
        + _video_enc_args()
        + ["-c:a", "aac", "-b:a", "192k", output],
        capture_output=True)


def join_clips(clips, output, vertical=False):
    """Concat clips and optionally reformat to 9:16 vertical."""
    out_dir  = os.path.dirname(output) or "."
    concat_f = os.path.join(out_dir, "_tmp_concat.txt")
    joined   = os.path.join(out_dir, "_tmp_joined.mp4")

    with open(concat_f, "w", encoding="ascii") as f:
        for c in clips:
            f.write(f"file '{c.replace(chr(92), '/')}'\n")

    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_f, "-c", "copy", joined],
                   capture_output=True)

    if vertical:
        vf = ("split=2[main][bg];"
              "[bg]scale=-2:1920,crop=1080:1920,boxblur=20:5[blurred];"
              "[main]scale=1080:607[fg];"
              "[blurred][fg]overlay=0:656")
        subprocess.run(
            [FFMPEG, "-y", "-i", joined, "-vf", vf]
            + _video_enc_args()
            + ["-c:a", "aac", "-b:a", "192k", output],
            capture_output=True)
        os.remove(joined)
    else:
        os.replace(joined, output)

    os.remove(concat_f)


# ─────────────────────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ValoHighlight")
        self.geometry("1120x740")
        self.minsize(960, 620)
        self.configure(fg_color="#0d0d0d")

        self.video_path   = ctk.StringVar()
        self.output_path  = ctk.StringVar()
        self.player_name  = ctk.StringVar(value="MrJunior")
        self.fmt_var      = ctk.StringVar(value="vertical")
        self.split_var    = ctk.StringVar(value="split")   # "split" or "single"
        self.round_gap    = ctk.IntVar(value=ROUND_GAP)

        self.rounds_data  = []   # list[list[float]]  — kills per round
        self.rounds_nums  = []   # list[int]          — actual in-game round number
        self._busy        = False
        self._medal_db    = _find_medal_db()

        self._build_ui()

    # ── UI ───────────────────────────────────────────────────

    def _build_ui(self):
        # ── Tab bar ──────────────────────────────────────────────
        tabbar = ctk.CTkFrame(self, fg_color="#111111", corner_radius=0, height=44)
        tabbar.pack(fill="x", side="top")
        tabbar.pack_propagate(False)

        ctk.CTkLabel(tabbar, text="ValoHighlight",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color="#00d4ff").pack(side="left", padx=16)

        # Encoder badge — detected once at startup
        enc_text  = "⚡ GPU · NVENC" if _check_nvenc() else "🖥 CPU · x264"
        enc_color = "#00cc66" if _check_nvenc() else "#ffaa00"
        ctk.CTkLabel(tabbar, text=enc_text,
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=enc_color).pack(side="right", padx=(0, 8))

        self.source_lbl = ctk.CTkLabel(tabbar, text="",
                                        font=ctk.CTkFont(size=11),
                                        text_color="gray60")
        self.source_lbl.pack(side="right", padx=16)

        # ── Body: left + right panels ────────────────────────────
        body = ctk.CTkFrame(self, fg_color="#0d0d0d", corner_radius=0)
        body.pack(fill="both", expand=True)

        # LEFT PANEL — inputs & options
        left_wrap = ctk.CTkFrame(body, fg_color="#111111", corner_radius=12, width=350)
        left_wrap.pack(side="left", fill="y", padx=(14, 7), pady=14)
        left_wrap.pack_propagate(False)
        left = ctk.CTkScrollableFrame(left_wrap, fg_color="transparent", corner_radius=0)
        left.pack(fill="both", expand=True)

        # RIGHT PANEL — results & generate
        right = ctk.CTkFrame(body, fg_color="#111111", corner_radius=12)
        right.pack(side="left", fill="both", expand=True, padx=(7, 14), pady=14)

        self._build_left(left)
        self._build_right(right)

    # ── Left panel ───────────────────────────────────────────────

    def _build_left(self, p):
        ctk.CTkLabel(p, text="Valorant Highlight",
                     font=ctk.CTkFont(size=22, weight="bold"),
                     text_color="white").pack(anchor="w", padx=18, pady=(18, 2))
        ctk.CTkLabel(p, text="Auto-detect kills and cut reels\nper round.",
                     font=ctk.CTkFont(size=12), text_color="#888888",
                     justify="left").pack(anchor="w", padx=18, pady=(0, 12))
        ctk.CTkFrame(p, height=1, fg_color="#2a2a2a").pack(fill="x", padx=18, pady=(0, 14))

        def lbl(text):
            ctk.CTkLabel(p, text=text, font=ctk.CTkFont(size=13, weight="bold"),
                         text_color="#cccccc").pack(anchor="w", padx=18, pady=(0, 4))

        # Video
        lbl("Video File")
        vrow = ctk.CTkFrame(p, fg_color="transparent")
        vrow.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkEntry(vrow, textvariable=self.video_path,
                     placeholder_text="Browse MP4 (OBS, Medal.tv…)",
                     height=36, fg_color="#1e1e1e",
                     border_color="#2a2a2a").pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(vrow, text="Browse", width=72, height=36,
                      fg_color="#1e1e2e", hover_color="#2a2a3e",
                      command=self._browse_video).pack(side="right")

        # Player name
        lbl("Your In-Game Name")
        ctk.CTkEntry(p, textvariable=self.player_name,
                     placeholder_text="e.g. MrJunior", height=36,
                     fg_color="#1e1e1e", border_color="#2a2a2a"
                     ).pack(fill="x", padx=18, pady=(0, 12))

        ctk.CTkFrame(p, height=1, fg_color="#2a2a2a").pack(fill="x", padx=18, pady=(0, 12))

        # Output format
        lbl("Output Format")
        fr = ctk.CTkFrame(p, fg_color="transparent")
        fr.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkRadioButton(fr, text="Vertical  9:16  (Shorts / Reels / TikTok)",
                            variable=self.fmt_var, value="vertical",
                            text_color="#bbbbbb").pack(anchor="w", pady=2)
        ctk.CTkRadioButton(fr, text="Horizontal  16:9  (YouTube)",
                            variable=self.fmt_var, value="horizontal",
                            text_color="#bbbbbb").pack(anchor="w", pady=2)

        # Output mode
        lbl("Output Mode")
        sp = ctk.CTkFrame(p, fg_color="transparent")
        sp.pack(fill="x", padx=18, pady=(0, 12))
        ctk.CTkRadioButton(sp, text="Split — one reel per round",
                            variable=self.split_var, value="split",
                            text_color="#bbbbbb").pack(anchor="w", pady=2)
        ctk.CTkRadioButton(sp, text="Single — one combined file",
                            variable=self.split_var, value="single",
                            text_color="#bbbbbb").pack(anchor="w", pady=2)

        ctk.CTkFrame(p, height=1, fg_color="#2a2a2a").pack(fill="x", padx=18, pady=(0, 12))

        # Clip info (fixed 4s — no slider needed)
        ctk.CTkLabel(p, text="Clip Duration",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#cccccc").pack(anchor="w", padx=18, pady=(0, 4))
        ctk.CTkLabel(p, text="2s before kill  +  kill  +  2s after  =  4s per clip",
                     font=ctk.CTkFont(size=11), text_color="#555555"
                     ).pack(anchor="w", padx=18, pady=(0, 12))

        # Round gap slider
        lbl("Round Gap (seconds)")
        gr = ctk.CTkFrame(p, fg_color="transparent")
        gr.pack(fill="x", padx=18, pady=(0, 2))
        self.gap_lbl = ctk.CTkLabel(gr, text=f"{ROUND_GAP}s", width=46,
                                     font=ctk.CTkFont(size=14, weight="bold"),
                                     text_color="#00d4ff")
        self.gap_lbl.pack(side="right")
        ctk.CTkSlider(gr, from_=25, to=70, number_of_steps=18, variable=self.round_gap,
                       command=lambda v: self.gap_lbl.configure(
                           text=f"{int(float(v))}s")).pack(fill="x", expand=True)
        ctk.CTkLabel(p, text="Kills further apart than this = different rounds",
                     font=ctk.CTkFont(size=10), text_color="#555555"
                     ).pack(anchor="w", padx=18, pady=(0, 12))

        ctk.CTkFrame(p, height=1, fg_color="#2a2a2a").pack(fill="x", padx=18, pady=(0, 12))

        # Output path
        lbl("Output Path")
        ctk.CTkLabel(p, text="Split → folder  |  Single → .mp4 file",
                     font=ctk.CTkFont(size=10), text_color="#555555"
                     ).pack(anchor="w", padx=18, pady=(0, 4))
        or_ = ctk.CTkFrame(p, fg_color="transparent")
        or_.pack(fill="x", padx=18, pady=(0, 18))
        ctk.CTkEntry(or_, textvariable=self.output_path,
                     placeholder_text="Browse output folder or file…",
                     height=36, fg_color="#1e1e1e",
                     border_color="#2a2a2a").pack(side="left", fill="x", expand=True, padx=(0, 6))
        ctk.CTkButton(or_, text="Browse", width=72, height=36,
                      fg_color="#1e1e2e", hover_color="#2a2a3e",
                      command=self._browse_output).pack(side="right")

    # ── Right panel ──────────────────────────────────────────────

    def _build_right(self, p):
        # Header row
        hdr = ctk.CTkFrame(p, fg_color="transparent")
        hdr.pack(fill="x", padx=18, pady=(18, 8))
        ctk.CTkLabel(hdr, text="Analysis Output",
                     font=ctk.CTkFont(size=18, weight="bold"),
                     text_color="white").pack(side="left")
        self.status_lbl = ctk.CTkLabel(hdr, text="Ready",
                                        font=ctk.CTkFont(size=12),
                                        text_color="#888888")
        self.status_lbl.pack(side="left", padx=(12, 0))

        # Analyze button (top-right)
        self.analyze_btn = ctk.CTkButton(
            hdr, text="Analyze Video", width=130, height=34,
            corner_radius=7, fg_color="#1a6ef5", hover_color="#1558cc",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_analyze)
        self.analyze_btn.pack(side="right")

        # Result text box (round/kill summary)
        self.result_box = ctk.CTkTextbox(
            p, height=120,
            fg_color="#1a1a1a", border_color="#2a2a2a", border_width=1,
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color="#aaaaaa", wrap="word")
        self.result_box.pack(fill="x", padx=18, pady=(0, 10))
        self._set_result("Select a video and click Analyze.")

        # Kill timestamps section
        ts_hdr = ctk.CTkFrame(p, fg_color="transparent")
        ts_hdr.pack(fill="x", padx=18, pady=(0, 4))
        ctk.CTkLabel(ts_hdr, text="Kill Timestamps",
                     font=ctk.CTkFont(size=14, weight="bold"),
                     text_color="white").pack(side="left")
        ctk.CTkLabel(ts_hdr,
                     text="Auto-filled — edit if needed (MM:SS)",
                     font=ctk.CTkFont(size=11), text_color="#555555"
                     ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(ts_hdr, text="Refresh Rounds", width=120, height=28,
                      corner_radius=6, fg_color="#1e1e1e", hover_color="#2a2a2a",
                      border_width=1, border_color="#333333",
                      text_color="#aaaaaa", font=ctk.CTkFont(size=11),
                      command=self._build_rounds).pack(side="right")

        self.kills_box = ctk.CTkTextbox(
            p, height=140,
            fg_color="#1a1a1a", border_color="#2a2a2a", border_width=1,
            font=ctk.CTkFont(family="Consolas", size=13),
            text_color="#00d4ff")
        self.kills_box.pack(fill="x", padx=18, pady=(0, 6))

        self.round_lbl = ctk.CTkLabel(p, text="",
                                       font=ctk.CTkFont(size=11),
                                       text_color="#555555")
        self.round_lbl.pack(anchor="w", padx=18, pady=(0, 10))

        # Progress bar
        self.progress = ctk.CTkProgressBar(p, height=8,
                                            progress_color="#1a6ef5",
                                            fg_color="#1e1e1e")
        self.progress.pack(fill="x", padx=18, pady=(0, 8))
        self.progress.set(0)

        # Generate button
        self.gen_btn = ctk.CTkButton(
            p, text="GENERATE HIGHLIGHT", height=46,
            corner_radius=8, fg_color="#1a6ef5", hover_color="#1558cc",
            font=ctk.CTkFont(size=16, weight="bold"), text_color="white",
            command=self._start_generate)
        self.gen_btn.pack(fill="x", padx=18, pady=(0, 14))

    def _sep(self, parent, text):
        pass  # no longer used — kept for safety

    # ── File pickers ─────────────────────────────────────────

    def _browse_video(self):
        path = filedialog.askopenfilename(
            title="Select Valorant Recording",
            filetypes=[("MP4 files", "*.mp4"), ("All files", "*.*")],
            initialdir=str(Path.home() / "Desktop"))
        if path:
            self.video_path.set(path)
            # Default: folder of the video (works for both split and single)
            self.output_path.set(str(Path(path).parent))

    def _browse_output(self):
        if self.split_var.get() == "split":
            path = filedialog.askdirectory(title="Choose output folder for reels")
        else:
            path = filedialog.asksaveasfilename(
                title="Save Highlight As", defaultextension=".mp4",
                filetypes=[("MP4 files", "*.mp4")])
        if path:
            self.output_path.set(path)

    # ── Analyze ──────────────────────────────────────────────

    def _start_analyze(self):
        video = self.video_path.get()
        if not video or not os.path.exists(video):
            messagebox.showerror("Error", "Please select a valid video file first.")
            return
        name = self.player_name.get().strip()
        if not name:
            messagebox.showerror("Error", "Please enter your in-game name.")
            return
        if self._busy:
            return
        self._busy = True
        self.analyze_btn.configure(state="disabled", text="Analyzing…")
        self.progress.set(0)
        threading.Thread(target=self._analyze_thread, daemon=True).start()

    def _analyze_thread(self):
        video    = self.video_path.get()
        name     = self.player_name.get().strip()
        kills    = []
        medal_raw = []  # all-player kills from DB (used for boundary detection)

        # ── Stage 1: get kills ───────────────────────────────────
        medal_raw = _kills_from_medal(video, self._medal_db)
        if medal_raw:
            n_raw = len(medal_raw)
            self.after(0, lambda: self.source_lbl.configure(
                text=f"DB: {n_raw} total kills found — verifying yours (GPU OCR)...",
                text_color="#ffaa00"))

            def verify_cb(p):
                self.after(0, lambda p=p: self.progress.set(p * 0.6))
                self.after(0, lambda i=int(p * n_raw): self._set_status(
                    f"OCR verifying kill {i}/{n_raw}..."))

            kills = verify_medal_kills(video, medal_raw, name, progress_cb=verify_cb)
            filtered = n_raw - len(kills)

            # Supplement: pixel-scan the video for any kills the DB missed
            self.after(0, lambda: self._set_status(
                "Supplemental scan: checking for kills DB missed..."))
            self.after(0, lambda: self.progress.set(0.62))

            def sup_cb(p):
                self.after(0, lambda p=p: self.progress.set(0.62 + p * 0.08))

            extra = supplement_with_pixel_scan(video, kills, name, progress_cb=sup_cb)
            if extra:
                kills = sorted(kills + extra)

            source_label = (
                f"Medal.tv: {len(kills) - len(extra)} from DB"
                + (f" + {len(extra)} supplemental" if extra else "")
                + f"  ({filtered} other-player kills filtered out)."
            )
        else:
            self.after(0, lambda: self.source_lbl.configure(
                text=f"No DB — OCR scanning for '{name}' kills (GPU)...",
                text_color="#ffaa00"))

            def ocr_cb(p):
                self.after(0, lambda p=p: self.progress.set(p * 0.6))
                stage = "scanning kill feed" if p < 0.5 else "reading names (GPU OCR)"
                self.after(0, lambda s=stage: self._set_status(f"Stage 1: {s}..."))

            try:
                kills, _ = scan_with_ocr(video, name, progress_cb=ocr_cb)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.after(0, lambda: self.analyze_btn.configure(
                    state="normal", text="Analyze Video"))
                self._busy = False
                return
            source_label = f"OCR: {len(kills)} kills found."

        if not kills:
            self.after(0, lambda: self._set_result(
                "No kills found. Check the player name and try again."))
            self.after(0, lambda: self.source_lbl.configure(
                text="No kills found.", text_color="gray60"))
            self.after(0, lambda: self.analyze_btn.configure(
                state="normal", text="Analyze Video"))
            self._busy = False
            return

        # ── Stage 2: detect round boundaries ────────────────────
        self.after(0, lambda: self.progress.set(0.75))
        boundary_method = ""

        # DB-gap only works when DB contains ALL players' kills (others: >0).
        # If DB only has your own kills (filtered==0 and all are yours),
        # gaps between your kills don't reliably mark round boundaries.
        db_has_all_players = medal_raw and (len(medal_raw) > len(kills) * 1.5)

        if db_has_all_players:
            self.after(0, lambda: self._set_status(
                "Stage 2: finding boundaries from all-player kill gaps..."))
            boundaries = find_boundaries_from_all_kills(medal_raw)
            boundary_method = f"DB-gap ({len(boundaries)} boundaries from {len(medal_raw)} total kills)"
        else:
            # DB only has your kills — use time-gap grouping directly (no boundary list needed)
            boundaries = []
            boundary_method = "time-gap (DB contains only your kills)"

        # ── Stage 3: assign kills to rounds ─────────────────────
        self.after(0, lambda: self.progress.set(0.9))
        self.after(0, lambda: self._set_status("Assigning kills to rounds..."))

        gap = self.round_gap.get()
        if boundaries:
            round_data       = assign_kills_to_rounds(kills, boundaries, gap=gap)
            self.rounds_data = [kts for _, kts in round_data]
            self.rounds_nums = [rn  for rn, _  in round_data]
        else:
            raw_rounds = group_rounds(kills, gap=gap)
            capped = []
            for r in raw_rounds:
                while len(r) > 5:
                    capped.append(r[:5]); r = r[5:]
                capped.append(r)
            self.rounds_data = capped
            self.rounds_nums = list(range(1, len(self.rounds_data) + 1))

        # ── Build per-round kill dict (yours + others) ───────────
        kill_dict = build_round_kill_dict(medal_raw, kills, boundaries) if medal_raw else {}

        multi      = [r for r in self.rounds_data if len(r) >= 2]
        kills_text = "\n".join(
            f"{int(t)//60:02d}:{t%60:05.2f}" for r in self.rounds_data for t in r)

        # ── Format analysis output ───────────────────────────────
        round_lines = []
        for rn, kts in zip(self.rounds_nums, self.rounds_data):
            times = ", ".join(f"{int(t)//60:02d}:{int(t)%60:02d}" for t in kts)
            rn_s  = f"{rn:2d}" if isinstance(rn, int) else str(rn)

            # Add others-kill count for this round if available
            rn_key = rn if isinstance(rn, int) else None
            others_info = ""
            if rn_key and rn_key in kill_dict:
                rd  = kill_dict[rn_key]
                others_info = f"  [total in round: {rd['total']} kills, others: {len(rd['others'])}]"

            round_lines.append(
                f"  Round {rn_s}: {len(kts)}K yours{others_info}  [{times}]")

        msg = (
            f"{source_label}\n"
            f"{len(kills)} kills in {len(self.rounds_data)} rounds "
            f"({len(multi)} multi-kill)\n"
            f"  Boundaries: {boundary_method}\n"
            f"{'─'*55}\n" +
            "\n".join(round_lines)
        )

        self.after(0, lambda: self.source_lbl.configure(
            text=source_label, text_color="#00cc66"))
        self.after(0, lambda: self._set_result(msg))
        self.after(0, lambda: self._fill_kills(kills_text))
        self.after(0, lambda: self.progress.set(1.0))
        self.after(0, lambda: self._set_status(
            f"{len(kills)} kills · {len(self.rounds_data)} rounds · ready"))
        self.after(0, lambda: self.analyze_btn.configure(
            state="normal", text="Analyze Video"))
        self._busy = False

    # ── Rounds ───────────────────────────────────────────────

    def _build_rounds(self):
        kills  = parse_timestamps(self.kills_box.get("0.0", "end"))
        # If we have round numbers from analysis, reuse them; otherwise re-group by time
        if self.rounds_nums and len(self.rounds_nums) == len(self.rounds_data):
            rounds = self.rounds_data  # already set by analyze
        else:
            gap    = self.round_gap.get()
            rounds = group_rounds(kills, gap=gap)
            self.rounds_data = rounds
            self.rounds_nums = list(range(1, len(rounds) + 1))
        multi  = [r for r in rounds if len(r) >= 2]
        streak = best_streak(rounds)
        self.round_lbl.configure(
            text=f"{len(kills)} kills  |  {len(rounds)} rounds  |  "
                 f"{len(multi)} multi-kill  |  best streak {len(streak)} rounds")

    # ── Generate ─────────────────────────────────────────────

    def _start_generate(self):
        if not self.video_path.get() or not os.path.exists(self.video_path.get()):
            messagebox.showerror("Error", "Please select a video file.")
            return
        out = self.output_path.get()
        if not out:
            messagebox.showerror("Error", "Please set an output path.")
            return
        split = self.split_var.get() == "split"
        if split:
            if not os.path.isdir(out):
                messagebox.showerror("Error",
                    "Split mode: please select an existing folder as output.\n"
                    "Click Browse and choose a folder.")
                return
        else:
            # Single mode: if user left path as a folder, auto-add filename
            if os.path.isdir(out):
                out = os.path.join(out, "video.mp4")
                self.output_path.set(out)
        self._build_rounds()
        if not self.rounds_data:
            messagebox.showerror("Error",
                "No kill timestamps found.\n"
                "Analyze the video or type timestamps in the Kill Timestamps box.")
            return
        self.gen_btn.configure(state="disabled", text="Generating…")
        self.progress.set(0)
        # Detect encoder before starting (runs once, cached after first call)
        enc_label = "GPU (NVENC)" if _check_nvenc() else "CPU (x264)"
        self._set_status(f"Encoder: {enc_label} — starting…")
        threading.Thread(target=self._generate_thread, daemon=True).start()

    def _generate_thread(self):
        video    = self.video_path.get()
        out_path = self.output_path.get()
        fmt      = self.fmt_var.get()
        split    = self.split_var.get() == "split"

        rounds   = self.rounds_data
        vertical = (fmt == "vertical")

        if not rounds:
            self.after(0, lambda: messagebox.showwarning("No Clips", "No kills found."))
            self.after(0, lambda: self.gen_btn.configure(
                state="normal", text="GENERATE HIGHLIGHT"))
            return

        # Resolve output folder
        out_dir = out_path if os.path.isdir(out_path) else os.path.dirname(out_path) or "."

        # Pair each round with its actual round number (may be int or str like "5b")
        rnums = self.rounds_nums if len(self.rounds_nums) == len(rounds) \
                else list(range(1, len(rounds) + 1))
        paired = list(zip(rnums, rounds))   # [(rnum, [kill_times]), ...]

        def rnum_str(rn):
            return f"{rn:02d}" if isinstance(rn, int) else str(rn)

        # Auto-categorise — keep round number for filename
        tasks = []   # (kill_times, filename_stem)

        # 3K+ rounds → one reel each  (3×4=12s … 5×4=20s)
        for rnum, r in paired:
            if len(r) >= 3:
                tasks.append((r, f"reel_{len(r)}k_round{rnum_str(rnum)}"))

        # 2K rounds → pair two rounds into one reel  (2+2=4 kills × 4s = 16s)
        twos = [(rnum, r) for rnum, r in paired if len(r) == 2]
        i = 0
        while i < len(twos):
            if i + 1 < len(twos):
                rnum_a, ka = twos[i]
                rnum_b, kb = twos[i + 1]
                combined = ka + kb
                tasks.append((combined,
                               f"reel_2x2k_round{rnum_str(rnum_a)}_{rnum_str(rnum_b)}"))
                i += 2
            else:
                # Lone 2K — keep as its own reel
                rnum, r = twos[i]
                tasks.append((r, f"reel_2k_round{rnum_str(rnum)}"))
                i += 1

        # 1K rounds → group 5 into one reel  (5×4=20s)
        ones = [(rnum, r) for rnum, r in paired if len(r) == 1]
        if ones:
            kills_1k = [r[0] for _, r in ones]
            j = 0
            while j < len(kills_1k):
                batch = kills_1k[j:j + 5]
                rnum  = ones[j][0]
                label = f"reel_5x1k_round{rnum_str(rnum)}" if len(batch) == 5 \
                        else f"reel_{len(batch)}x1k_round{rnum_str(rnum)}"
                tasks.append((batch, label))
                j += 5

        if not tasks:
            self.after(0, lambda: messagebox.showwarning("No Clips", "No kills to process."))
            self.after(0, lambda: self.gen_btn.configure(
                state="normal", text="GENERATE HIGHLIGHT"))
            return

        total_cuts  = sum(len(t[0]) for t in tasks)
        cut_counter = [0]

        def cut_group(kill_times):
            # Fixed: 2s before kill + 2s after = 4s per clip
            LEAD = 2
            DUR  = 4

            clips = []
            for kt in kill_times:
                i = cut_counter[0]
                cut_counter[0] += 1
                self.after(0, lambda p=(i + .5) / (total_cuts + 1): self.progress.set(p))
                self.after(0, lambda i=i, n=total_cuts:
                           self._set_status(f"Cutting clip {i+1}/{n}…"))
                ss  = max(0, round(kt) - LEAD)
                tmp = os.path.join(out_dir, f"_valo_tmp_{i:03d}.mp4")
                cut_clip(video, ss, DUR, tmp)
                if os.path.exists(tmp):
                    clips.append(tmp)
            return clips

        if split:
            # ── Split: one reel per round, auto-named ──────────
            saved_files = []

            for kill_times, stem in tasks:
                self.after(0, lambda s=stem: self._set_status(f"Processing {s}..."))

                temp_clips = cut_group(kill_times)
                if not temp_clips:
                    continue

                reel_path = os.path.join(out_dir, f"{stem}.mp4")
                self.after(0, lambda: self._set_status("Encoding reel..."))
                join_clips(temp_clips, reel_path, vertical=vertical)

                for f in temp_clips:
                    if os.path.exists(f): os.remove(f)

                if os.path.exists(reel_path):
                    saved_files.append(reel_path)

            self.after(0, lambda: self.progress.set(1.0))
            if saved_files:
                total_mb = sum(os.path.getsize(f) for f in saved_files) / 1024 / 1024
                names    = "\n".join(os.path.basename(f) for f in saved_files)
                self.after(0, lambda: self._set_status(
                    f"{len(saved_files)} reels  |  {total_mb:.1f} MB total"))
                self.after(0, lambda: messagebox.showinfo(
                    "Done!",
                    f"{len(saved_files)} reels saved to:\n{out_dir}\n\n{names}\n\n"
                    f"Total: {total_mb:.1f} MB"))
            else:
                self.after(0, lambda: messagebox.showerror("Error", "No reels were created."))

        else:
            # ── Single: join all kills into video.mp4 ──────────
            all_kill_times = [kt for kts, _ in tasks for kt in kts]
            all_clips      = cut_group(all_kill_times)

            if not all_clips:
                self.after(0, lambda: messagebox.showerror("Error", "Clip cutting failed."))
                self.after(0, lambda: self.gen_btn.configure(
                    state="normal", text="GENERATE HIGHLIGHT"))
                return

            self.after(0, lambda: self._set_status("Joining and encoding…"))
            self.after(0, lambda: self.progress.set(0.9))
            join_clips(all_clips, out_path, vertical=vertical)

            for f in all_clips:
                if os.path.exists(f): os.remove(f)

            self.after(0, lambda: self.progress.set(1.0))
            if os.path.exists(out_path):
                mb = os.path.getsize(out_path) / 1024 / 1024
                self.after(0, lambda: self._set_status(f"{len(all_clips)} clips  |  {mb:.1f} MB"))
                self.after(0, lambda: messagebox.showinfo(
                    "Done!", f"Highlight saved!\n\n{out_path}\n{mb:.1f} MB"))
            else:
                self.after(0, lambda: messagebox.showerror("Error", "Output file not created."))

        self.after(0, lambda: self.gen_btn.configure(
            state="normal", text="GENERATE HIGHLIGHT"))

    # ── Helpers ──────────────────────────────────────────────

    def _set_result(self, text):
        self.result_box.configure(state="normal")
        self.result_box.delete("0.0", "end")
        self.result_box.insert("0.0", text)
        self.result_box.configure(state="disabled")

    def _fill_kills(self, text):
        self.kills_box.delete("0.0", "end")
        self.kills_box.insert("0.0", text)

    def _set_status(self, msg):
        self.status_lbl.configure(text=msg)


if __name__ == "__main__":
    app = App()
    app.mainloop()

