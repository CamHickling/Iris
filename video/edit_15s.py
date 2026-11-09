"""Edit the 15-second silent Iris clip (video/shotlist-15s.md).

Reads the takes from video/build/raw15/ (made by record_15s.py) and the demo
session they produced, and renders every frame with Pillow. Each shot is a
headline over a framed app window on the brand background, with short eased
transitions between shots. Fonts are loaded from C:/Windows/Fonts (Segoe UI
Variable, Consolas), so ffmpeg's drawtext isn't needed.

Outputs:
    video/iris-15s.mp4    1920x1080, 30 fps, exactly 15.000 s, no audio
    video/iris-15s.gif    960 px wide, 12 fps, palettegen, checked < 10 MB
    video/iris-thumb.png  1920x1080 still (the results frame)

Usage (from the repo root, in an environment with opencv-python and Pillow):

    python video/edit_15s.py
    python video/edit_15s.py --preview   # also writes a contact sheet to video/build/raw15/
"""

import argparse
import json
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO = Path(__file__).resolve().parent.parent
RAW = REPO / "video" / "build" / "raw15"
OUT_MP4 = REPO / "video" / "iris-15s.mp4"
OUT_GIF = REPO / "video" / "iris-15s.gif"
OUT_THUMB = REPO / "video" / "iris-thumb.png"
LOGO = REPO / "assets" / "brand" / "iris-logo-dark.png"

W, H, FPS = 1920, 1080, 30
TOTAL = 15.0
FONTS = Path("C:/Windows/Fonts")

NAVY = (11, 11, 34)
GLOW = (70, 52, 170)
PURPLE = (123, 106, 249)
LAVENDER = (165, 152, 255)
WHITE = (255, 255, 255)
GREY = (150, 152, 185)
FOOT_GREY = (205, 205, 228)
PANEL = (22, 22, 58)

# Window area under the headline: content is scaled to fit this box, centred on WIN_CY
BOX_W, BOX_H, WIN_CY = 1720, 640, 655
TRANS = 0.35   # seconds for a shot or headline to ease in

# Source crops (x0, y0, x1, y1) in the 1920x1080 takes
DEVICES = (25, 370, 905, 765)        # Device Status panel, Experiment tab before the run
CHECKLIST = (25, 598, 1425, 925)     # Warmup: checklist, WAITING, REC + timer, Continue
PERFORMANCE = (560, 235, 1845, 825)  # Performance title, REC + timer, Continue

# Each shot: output start/end and what fills the window.
#   take/src: source take and time; freeze holds one frame;
#   speed/span: play `span` s of source at `speed`, then hold the last frame.
# Measured in the devices take: all rows "Checking..." by 1.68 s, all green by 3.5 s.
SHOTS = [
    dict(start=0.0, end=3.0, take="devices", src=0.5, freeze=True, crop=DEVICES),  # before Calibrate
    dict(start=3.0, end=7.0, take="checklist", src=0.9, crop=CHECKLIST),
    dict(start=7.0, end=9.0, take="devices", src=1.68, speed=1.37, span=1.92, crop=DEVICES),
    dict(start=9.0, end=10.6, take="rec", src=1.6, crop=PERFORMANCE),
    dict(start=10.6, end=12.2, take=None, card="manifest"),
    dict(start=12.2, end=15.0, take=None, card="stats"),
]

# (start, end, kicker, headline): what a muted viewer reads
HEADLINES = [
    (0.0, 3.0, "THE PROBLEM", "6 devices. One missed step = lost data."),
    (3.0, 7.0, "THE FIX", "Each step unlocks only after its prerequisites"),
    (7.0, 12.2, "ONE TIMELINE", "Every device logged to one shared timeline"),
    (12.2, 15.0, "THE RESULT", "Zero lost sessions"),
]

STATS = [("50", "participants"), ("60+", "trials"), ("1+ TB", "captured")]
FOOTER = "Built for UBC researchers studying flow in athletes"


# --- Helpers ---

def vfont(size, style):
    """Segoe UI Variable at a named instance, e.g. 'Bold Display', 'Semibold Text'."""
    f = ImageFont.truetype(str(FONTS / "SegUIVar.ttf"), size)
    f.set_variation_by_name(style)
    return f


def mono(size, bold=False):
    return ImageFont.truetype(str(FONTS / ("consolab.ttf" if bold else "consola.ttf")), size)


def ease_out(p):
    p = min(max(p, 0.0), 1.0)
    return 1 - (1 - p) ** 3


def background():
    bg = Image.new("RGB", (W, H), NAVY)
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse((W * 0.2, H * 0.4, W * 1.0, H * 1.5), fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(240)).point(lambda v: int(v * 0.5))
    bg.paste(GLOW, (0, 0), glow)
    return bg


@lru_cache(maxsize=None)
def logo(width):
    im = Image.open(LOGO).convert("RGBA")
    return im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)


def paste_logo(canvas, width, x, y, alpha=1.0):
    if alpha <= 0:
        return
    im = logo(width)
    mask = im.getchannel("A")
    if alpha < 1:
        mask = mask.point(lambda v: int(v * alpha))
    canvas.paste(im, (int(x), int(y)), mask)


def paste_corner_logo(canvas, alpha=1.0):
    im = logo(200)
    paste_logo(canvas, 200, W - im.width - 48, H - im.height - 34, alpha)


@lru_cache(maxsize=None)
def window_masks(w, h, radius=20):
    """Rounded-corner mask, soft drop shadow and a faint border for a w x h window."""
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius, fill=255)
    pad = 90
    shadow = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((pad, pad + 26, pad + w, pad + h + 26), radius, fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(38))
    border = Image.new("L", (w, h), 0)
    ImageDraw.Draw(border).rounded_rectangle((0, 0, w - 1, h - 1), radius, outline=60, width=2)
    return mask, shadow, border, pad


def fit(crop_w, crop_h):
    s = min(BOX_W / crop_w, BOX_H / crop_h)
    return int(round(crop_w * s)), int(round(crop_h * s))


def place_window(canvas, content, alpha=1.0, scale=1.0, dy=0.0):
    """Paste content as a framed window centred in the window area."""
    if alpha <= 0:
        return
    if scale != 1.0:
        content = content.resize((int(content.width * scale), int(content.height * scale)),
                                 Image.BILINEAR)
    w, h = content.size
    x, y = (W - w) // 2, int(WIN_CY - h / 2 + dy)
    mask, shadow, border, pad = window_masks(w, h)
    a = lambda m: m if alpha >= 1 else m.point(lambda v: int(v * alpha))  # noqa: E731
    canvas.paste((0, 0, 0), (x - pad, y - pad), a(shadow))
    canvas.paste(content, (x, y), a(mask))
    canvas.paste((255, 255, 255), (x, y), a(border))


class Take:
    def __init__(self, name):
        cap = cv2.VideoCapture(str(RAW / f"{name}.mp4"))
        if not cap.isOpened():
            raise SystemExit(f"Missing take {name}.mp4 - run video/record_15s.py first")
        self.frames = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            self.frames.append(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)))

    def at(self, t):
        return self.frames[min(int(round(t * FPS)), len(self.frames) - 1)]


# --- Generated content ---

def manifest_card(session: Path) -> Image.Image:
    """Session folder + the device start events in sync_manifest.json (no full paths)."""
    w, h = 1600, 620
    img = Image.new("RGB", (w, h), PANEL)
    d = ImageDraw.Draw(img)
    head, sub = vfont(40, "Semibold Display"), vfont(28, "Regular")
    m_f, m_b, label_f = mono(34), mono(34, True), vfont(25, "Regular")

    x0, y0 = 56, 48
    d.text((x0, y0), "Session folder", font=head, fill=WHITE)
    y = y0 + 78
    for p in sorted(session.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if p.name == "console_log.txt":
            continue
        name = p.name + ("/" if p.is_dir() else "")
        if name == "sync_manifest.json":
            y += 6
            tw = d.textlength(name, font=m_b)
            d.rounded_rectangle((x0 - 10, y - 6, x0 + tw + 10, y + 44), 8, fill=PURPLE)
            d.text((x0, y), name, font=m_b, fill=WHITE)
        else:
            d.text((x0, y), name, font=m_f, fill=GREY)
        y += 52

    m = json.loads((session / "sync_manifest.json").read_text())
    want = {"hr_recording_start": "Polar H10 heart rate",
            "overhead_recorder_start": "Overhead webcam",
            "gopro_recording_start": "GoPros",
            "face_recorder_start": "Face webcam",
            "audio_recorder_start": "Microphone"}
    rows, seen = [], set()
    for e in m["events"]:
        ev = e["event"]
        if ev in want and ev not in seen:
            if ev == "gopro_recording_start" and e.get("purpose") != "performance":
                continue
            seen.add(ev)
            t = datetime.fromtimestamp(e["wall_time"]).strftime("%H:%M:%S.%f")[:-3]
            rows.append((t, ev, want[ev]))
    rows.sort()
    px = 560
    d.text((px, y0), "sync_manifest.json", font=head, fill=WHITE)
    d.text((px + d.textlength("sync_manifest.json", font=head) + 20, y0 + 10),
           "one clock, every device", font=sub, fill=GREY)
    d.line((px - 40, y0, px - 40, h - y0), fill=(50, 50, 96), width=2)
    y = y0 + 84
    for t, ev, label in rows:
        d.text((px, y), t, font=m_b, fill=LAVENDER)
        d.text((px + 270, y), ev, font=m_f, fill=WHITE)
        d.text((px + 270, y + 42), label, font=label_f, fill=GREY)
        y += 92
    return img


def draw_stats(canvas, t_local):
    """Three stat figures that rise in one after another, plus the footer line."""
    d = ImageDraw.Draw(canvas)
    num_f, lab_f, foot_f = vfont(150, "Bold Display"), vfont(40, "Regular"), vfont(40, "Regular")
    col_w = 520
    x_start = (W - col_w * len(STATS)) / 2
    for i, (num, label) in enumerate(STATS):
        p = ease_out((t_local - 0.15 - 0.15 * i) / TRANS)
        if p <= 0:
            continue
        layer = Image.new("RGBA", (col_w, 280), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.text(((col_w - ld.textlength(num, font=num_f)) / 2, 0), num, font=num_f, fill=WHITE)
        ld.text(((col_w - ld.textlength(label, font=lab_f)) / 2, 200), label, font=lab_f,
                fill=LAVENDER)
        if p < 1:
            layer.putalpha(layer.getchannel("A").point(lambda v: int(v * p)))
        canvas.paste(layer, (int(x_start + col_w * i), int(345 + 30 * (1 - p))), layer)
    # Iris logo and the footer line ease in after the stats
    p = ease_out((t_local - 0.7) / TRANS)
    if p > 0:
        lw = 380
        paste_logo(canvas, lw, (W - lw) / 2, 715 + 20 * (1 - p), p)
        fw = d.textlength(FOOTER, font=foot_f)
        shade = tuple(int(NAVY[k] + (FOOT_GREY[k] - NAVY[k]) * p) for k in range(3))
        d.text(((W - fw) / 2, 870 + 20 * (1 - p)), FOOTER, font=foot_f, fill=shade)
    d.line(((W - 120) / 2, 278, (W + 120) / 2, 278), fill=PURPLE, width=4)


# --- Headlines ---

def headline_layer(kicker, text):
    layer = Image.new("RGBA", (W, 260), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    size = 78
    while True:
        h_f = vfont(size, "Bold Display")
        if d.textlength(text, font=h_f) <= W - 200 or size <= 56:
            break
        size -= 2
    k_f, track = vfont(28, "Semibold Text"), 5  # kicker with light letter-spacing
    kw = sum(d.textlength(c, font=k_f) + track for c in kicker) - track
    x = (W - kw) / 2
    for c in kicker:
        d.text((x, 80), c, font=k_f, fill=LAVENDER)
        x += d.textlength(c, font=k_f) + track
    d.text(((W - d.textlength(text, font=h_f)) / 2, 122), text, font=h_f, fill=WHITE)
    return layer


def paste_fade(canvas, layer, alpha, dy=0):
    if alpha <= 0:
        return
    if alpha < 1:
        layer = layer.copy()
        layer.putalpha(layer.getchannel("A").point(lambda v: int(v * alpha)))
    canvas.paste(layer, (0, int(dy)), layer)


# --- Render ---

def render(session):
    bg = background()
    takes = {s["take"]: Take(s["take"]) for s in SHOTS if s["take"]}
    cards = {"manifest": manifest_card(session)}
    heads = [(a, b, headline_layer(k, t)) for a, b, k, t in HEADLINES]

    def window_content(shot, t):
        if shot.get("card") == "manifest":
            return cards["manifest"]
        local = t - shot["start"]
        played = 0 if shot.get("freeze") else local * shot.get("speed", 1.0)
        src_t = shot["src"] + min(played, shot.get("span", played))
        crop = shot["crop"]  # fixed framing: no zoom inside a shot
        size = fit(shot["crop"][2] - shot["crop"][0], shot["crop"][3] - shot["crop"][1])
        return takes[shot["take"]].at(src_t).resize(size, Image.LANCZOS, box=crop)

    for i in range(int(TOTAL * FPS)):
        t = i / FPS
        canvas = bg.copy()
        idx = next(k for k, s in enumerate(SHOTS) if s["start"] <= t < s["end"])
        shot = SHOTS[idx]
        local = t - shot["start"]
        p = ease_out(local / TRANS) if idx > 0 else 1.0

        # Outgoing window fades out while the new one eases in
        if p < 1 and SHOTS[idx - 1].get("card") != "stats":
            prev = SHOTS[idx - 1]
            place_window(canvas, window_content(prev, prev["end"] - 1e-3), alpha=1 - p,
)
        if shot.get("card") == "stats":
            paste_corner_logo(canvas, 1 - p)  # hands over to the large logo
            draw_stats(canvas, local)
        else:
            paste_corner_logo(canvas)
            place_window(canvas, window_content(shot, t), alpha=p, dy=18 * (1 - p))

        for a, b, layer in heads:
            if a <= t < b:
                hp = ease_out((t - a) / TRANS) if a > 0 else 1.0
                paste_fade(canvas, layer, hp, dy=16 * (1 - hp))
            elif b <= t < b + 0.15 and b < TOTAL:
                paste_fade(canvas, layer, 1 - (t - b) / 0.15)
        yield canvas


def run(cmd):
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--preview", action="store_true", help="write a contact sheet too")
    args = parser.parse_args()

    session = Path((RAW / "session.txt").read_text().strip())
    enc = subprocess.Popen(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
         "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "animation",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(OUT_MP4)],
        stdin=subprocess.PIPE)
    thumb_frame = int(14.5 * FPS)
    sheet = []
    for i, frame in enumerate(render(session)):
        enc.stdin.write(np.asarray(frame).tobytes())
        if i == thumb_frame:
            frame.save(OUT_THUMB)
        if args.preview and i % 15 == 0:
            sheet.append(frame.resize((480, 270), Image.LANCZOS))
    enc.stdin.close()
    if enc.wait() != 0:
        raise SystemExit("ffmpeg failed")

    palette = RAW / "palette.png"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(OUT_MP4), "-vf",
         "fps=12,scale=960:-1:flags=lanczos,palettegen=max_colors=256:stats_mode=full",
         str(palette)])
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(OUT_MP4),
         "-i", str(palette), "-lavfi",
         "fps=12,scale=960:-1:flags=lanczos[v];[v][1:v]paletteuse=dither=bayer:bayer_scale=3:"
         "diff_mode=rectangle", str(OUT_GIF)])

    if args.preview:
        cols = 6
        rows = (len(sheet) + cols - 1) // cols
        cs = Image.new("RGB", (480 * cols, 270 * rows))
        for k, f in enumerate(sheet):
            cs.paste(f, ((k % cols) * 480, (k // cols) * 270))
        cs.save(RAW / "contact_sheet.png")

    gif_mb = OUT_GIF.stat().st_size / 1e6
    print(f"{OUT_MP4.name}: {OUT_MP4.stat().st_size / 1e6:.1f} MB")
    print(f"{OUT_GIF.name}: {gif_mb:.1f} MB" + ("  (OVER 10 MB)" if gif_mb >= 10 else ""))
    print(f"{OUT_THUMB.name}: frame at 14.5 s")


if __name__ == "__main__":
    main()
