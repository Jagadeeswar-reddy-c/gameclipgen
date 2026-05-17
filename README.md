# GameClipGen

> **Automatically detect kills, group rounds, and generate highlight reels — from any game recording.**

GameClipGen is a local Windows desktop app that turns raw gameplay recordings into ready-to-post short-form reels (Shorts / Reels / TikTok) with zero manual clipping. Point it at a video, hit **Analyze**, and it finds every kill, groups them by round, and exports named highlight files like `reel_4k_round06.mp4` or `reel_2x2k_round03_07.mp4`.

---

## Current Game Support

| Game | Detection Method | Status |
|------|-----------------|--------|
| Valorant | Medal.tv DB + GPU OCR + pixel scan | ✅ Supported |
| More games | — | 🚧 Planned |

---

## Features

- **Auto kill detection** — reads your Medal.tv database for instant results; falls back to full GPU-accelerated OCR scan for OBS/Shadowplay recordings
- **Supplemental pixel scan** — a fast pixel-level sweep catches any kills the database missed (teal killer name + red victim name detection)
- **Per-round grouping** — kills separated by > 40 s are split into separate rounds automatically
- **Smart reel packaging**
  - 3K / 4K / 5K rounds → one reel each
  - Two 2K rounds → combined into one 16 s reel
  - Up to five 1K rounds → batched into one 20 s reel
- **Auto-named output** — `reel_3k_round05.mp4`, `reel_2x2k_round03_08.mp4`, `reel_5x1k_round01.mp4`
- **Vertical 9:16 or Horizontal 16:9** — blurred-background letterbox for Shorts / Reels / TikTok
- **GPU-accelerated OCR** — uses EasyOCR with CUDA; auto-falls back to CPU if no GPU
- **Editable timestamps** — kill timestamps are shown in a text box so you can add, remove, or correct before generating
- **Dark UI** — built with customtkinter, no install wizard needed

---

## How It Works

```
Video file
    │
    ▼
① Medal.tv DB scan          (instant — parses local SQLite database)
    │
    ▼
② OCR verification           (GPU EasyOCR — confirms YOUR kills vs others')
    │
    ▼
③ Supplemental pixel scan    (2 fps sweep — finds kills the DB missed)
    │
    ▼
④ Round grouping             (40 s gap = new round)
    │
    ▼
⑤ Clip cutting               (2 s before kill + 2 s after = 4 s per clip)
    │
    ▼
⑥ Reel assembly + export     (ffmpeg concat → vertical/horizontal encode)
```

---

## Requirements

| Dependency | Version | Purpose |
|------------|---------|---------|
| Python | 3.10+ | Runtime |
| ffmpeg | Any recent | Video cutting & encoding |
| customtkinter | 5.x | GUI |
| easyocr | 1.7+ | Kill-feed name recognition |
| PyTorch | 2.x (CUDA optional) | EasyOCR backend |

### Install Python dependencies

```bash
pip install customtkinter easyocr
```

For GPU acceleration (recommended — ~5× faster OCR):

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

### Install ffmpeg

Download from [ffmpeg.org](https://ffmpeg.org/download.html) and place `ffmpeg.exe` + `ffprobe.exe` at:

```
C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe
```

---

## Usage

```bash
python valo_highlight_app.py
```

1. **Browse** — select your `.mp4` recording (Medal.tv or OBS)
2. **Your In-Game Name** — enter exactly as it appears in the kill feed (e.g. `MrJunior`)
3. **Analyze Video** — detects all your kills automatically
4. Review the kill timestamps — edit if needed
5. Choose **Output Format** (Vertical 9:16 or Horizontal 16:9)
6. Choose **Output Mode** (Split = one reel per round, Single = one combined file)
7. **GENERATE HIGHLIGHT** — reels are saved to the video's folder

---

## Output File Naming

| Round result | Output filename |
|-------------|-----------------|
| 5 kills in Round 3 | `reel_5k_round03.mp4` |
| 4 kills in Round 6 | `reel_4k_round06.mp4` |
| 3 kills in Round 12 | `reel_3k_round12.mp4` |
| 2 kills in Rounds 4 & 9 | `reel_2x2k_round04_09.mp4` |
| 1 kill each in Rounds 1-5 | `reel_5x1k_round01.mp4` |

---

## Clip Timing

Each kill gets a **4-second clip**:

```
 ──────────────────────────────────
  2 s before kill │ kill │ 2 s after
 ──────────────────────────────────
```

| Kill count | Reel duration |
|-----------|--------------|
| 3K | 12 s |
| 4K | 16 s |
| 5K | 20 s |
| 2K + 2K | 16 s |
| 5× 1K | 20 s |

---

## Project Structure

```
gameclipgen/
├── valo_highlight_app.py   # Main app — Valorant highlight generator
└── README.md
```

---

## Roadmap

- [ ] CS2 / CS:GO support
- [ ] Apex Legends support
- [ ] Automatic game detection from video metadata
- [ ] Configurable clip lead/trail time
- [ ] Thumbnail auto-generation
- [ ] Direct upload to YouTube Shorts / TikTok
- [ ] Batch processing (multiple videos at once)

---

## Contributing

Pull requests are welcome. To add support for a new game:

1. Implement a kill-detection function (pixel scan, OCR, or game-specific DB)
2. Define the kill-feed crop coordinates for that game's HUD
3. Add the game to the source selector in the UI

---

## License

MIT
