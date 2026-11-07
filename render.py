#!/usr/bin/env python3
"""Adhuro Katha renderer.

Background video + Nepali quote -> 1080x1920 slow-motion mp4 -> Telegram (with
approval buttons) -> report back to the Apps Script backend.

All settings come from environment variables (set by the GitHub workflow).
"""

import glob
import json
import os
import subprocess
import sys
import time
import traceback

import requests
from PIL import (Image, ImageChops, ImageDraw, ImageFilter, ImageFont,
                 ImageOps, ImageStat, features)

W, H = 1080, 1920
FPS = 30
DURATION = 20          # final video length in seconds
MIN_SLOW = 1.5         # footage is always slowed down at least this much
WORK = "work"
HASHTAGS = "#अधुरोकथा #Loneliness #NepaliSadQuotes #DeepFeelings #BrokenHeart"
UA = {"User-Agent": "Mozilla/5.0 (AdhuroKathaRenderer)"}

# Text look
FONT_MAX, FONT_MIN = 54, 36   # font size range in px
TEXT_WIDTH = 860              # max line width in px
TOP_Y = 0.115                 # text block starts at 11.5% of the height
MOON = True                   # small crescent after the last line
MOON_COLOR = (246, 211, 101)


# --------------------------------------------------------------------------
# Text layout and image
# --------------------------------------------------------------------------

def find_font():
    override = os.environ.get("FONT_PATH")
    if override and os.path.exists(override):
        return override

    patterns = [
        "/usr/share/fonts/**/NotoSerifDevanagari-Regular.ttf",
        "/usr/share/fonts/**/NotoSerifDevanagari*.ttf",
        "/usr/share/fonts/**/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/**/NotoSansDevanagari*.ttf",
    ]
    for pattern in patterns:
        found = sorted(glob.glob(pattern, recursive=True))
        if found:
            return found[0]
    raise RuntimeError("No Devanagari font found (install fonts-noto-core).")


def load_font(path, size):
    # Without RAQM, Devanagari conjuncts are drawn wrongly, so refuse to run.
    if not features.check("raqm"):
        raise RuntimeError("Pillow has no RAQM support (need libraqm/fribidi).")
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)


def wrap_lines(text, font, max_width):
    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            continue
        current = words[0]
        for word in words[1:]:
            trial = current + " " + word
            if font.getlength(trial) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def layout_text(quote):
    """Chooses font size and line breaks. Prefers 2 lines, at most 3."""
    quote = quote.strip().strip('"“”').strip()
    font_path = find_font()

    # Prefer a line break right after the first comma, like a poem:
    #   "...छैन,"
    #   "तर मनले ..."
    clause_quote = quote.replace(", ", ",\n", 1) if "\n" not in quote else quote

    chosen = None
    for size in range(FONT_MAX, FONT_MIN - 1, -2):
        font = load_font(font_path, size)
        for text in (clause_quote, quote):
            lines = wrap_lines(text, font, TEXT_WIDTH)
            if len(lines) <= 3:
                chosen = (size, font, lines)
                break
        if chosen:
            break
    if chosen is None:  # very long quote: smallest size, any number of lines
        font = load_font(font_path, FONT_MIN)
        chosen = (FONT_MIN, font, wrap_lines(quote, font, TEXT_WIDTH))

    size, font, lines = chosen
    line_h = int(size * 1.45)
    moon_r = int(size * 0.42)
    moon_gap = int(size * 0.35)
    top = int(H * TOP_Y)

    # x position of every line (the last one leaves room for the moon)
    positions = []
    for i, line in enumerate(lines):
        width = font.getlength(line)
        if MOON and i == len(lines) - 1:
            width += moon_gap + 2 * moon_r
        positions.append((W - width) / 2)

    total_h = line_h * len(lines)
    x0 = max(0, int(min(positions)) - 40)
    x1 = min(W, int(max(p + font.getlength(l) for p, l in zip(positions, lines))) + 120)
    bbox = (x0, max(0, top - 30), x1, top + total_h + 30)

    return {
        "font": font, "lines": lines, "size": size, "line_h": line_h,
        "top": top, "positions": positions, "bbox": bbox,
        "moon_r": moon_r, "moon_gap": moon_gap,
    }


def moon_mask(radius):
    """Crescent shape as a smooth (supersampled) mask."""
    s = 4
    size = int(2 * radius * s) + 8 * s
    c = size / 2
    r = radius * s

    full = Image.new("L", (size, size), 0)
    ImageDraw.Draw(full).ellipse([c - r, c - r, c + r, c + r], fill=255)

    cut = Image.new("L", (size, size), 0)
    r2 = r * 0.82
    cx, cy = c + r * 0.45, c - r * 0.28
    ImageDraw.Draw(cut).ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=255)

    mask = ImageChops.subtract(full, cut)
    return mask.resize((size // s, size // s), Image.LANCZOS)


def make_text_png(layout, luma, out_path):
    """Draws the quote on a transparent 1080x1920 image.

    Bright footage gets dark text; dark footage gets white text.
    """
    dark_text = luma > 150
    font, lines = layout["font"], layout["lines"]

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if not dark_text:
        # soft shade at the top so white text stays readable on any footage
        grad = ImageOps.invert(Image.linear_gradient("L")).resize((W, int(H * 0.38)))
        grad = grad.point(lambda v: int(v * 85 / 255))
        band = Image.new("RGBA", grad.size, (0, 0, 0, 0))
        band.putalpha(grad)
        canvas.alpha_composite(band, (0, 0))

    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hdraw, tdraw = ImageDraw.Draw(halo), ImageDraw.Draw(text)

    if dark_text:
        fill, halo_fill, offset, blur = (22, 28, 42, 255), (255, 255, 255, 150), (0, 0), 9
    else:
        fill, halo_fill, offset, blur = (255, 255, 255, 250), (0, 0, 0, 190), (2, 4), 8

    y = layout["top"]
    last_end = (0, 0)
    for line, x in zip(lines, layout["positions"]):
        hdraw.text((x + offset[0], y + offset[1]), line, font=font, fill=halo_fill)
        tdraw.text((x, y), line, font=font, fill=fill)
        last_end = (x + font.getlength(line), y)
        y += layout["line_h"]

    halo = halo.filter(ImageFilter.GaussianBlur(blur))

    if MOON:
        r = layout["moon_r"]
        mask = moon_mask(r)
        mx = int(last_end[0] + layout["moon_gap"])
        my = int(last_end[1] + layout["size"] * 0.78 - mask.size[1] / 2)
        solid = Image.new("RGBA", mask.size, MOON_COLOR + (255,))
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glow.paste(Image.new("RGBA", mask.size, MOON_COLOR + (120,)), (mx, my), mask)
        glow = glow.filter(ImageFilter.GaussianBlur(10))
        halo = Image.alpha_composite(halo, glow)
        text.paste(solid, (mx, my), mask)

    canvas = Image.alpha_composite(canvas, halo)
    canvas = Image.alpha_composite(canvas, text)
    canvas.save(out_path)


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------

def download(url, path):
    with requests.get(url, headers=UA, stream=True, timeout=90) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    size = os.path.getsize(path)
    if size < 50_000:
        raise RuntimeError("Downloaded background is too small (%d bytes)" % size)


def probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def plan_speed(source_duration):
    """Slow-motion factor so the footage fills DURATION seconds."""
    factor = max(MIN_SLOW, DURATION / source_duration)
    used = min(source_duration, DURATION / factor)
    return factor, used


def sample_luma(bg_path, bbox, used):
    """Average brightness (0-255) of the area behind the text."""
    values = []
    for i, frac in enumerate((0.1, 0.5, 0.9)):
        frame = os.path.join(WORK, "luma_%d.png" % i)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", "%.2f" % (used * frac), "-i", bg_path, "-frames:v", "1",
                 "-vf", "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d"
                        % (W, H, W, H), frame],
                check=True,
            )
            region = Image.open(frame).convert("L").crop(bbox)
            values.append(ImageStat.Stat(region).mean[0])
        except (subprocess.CalledProcessError, OSError):
            continue
    return sum(values) / len(values) if values else 80.0


def make_video(bg_path, text_png, out_path, factor, used, crf=21):
    filter_complex = (
        # footage: crop to 9:16, slow down, smooth the motion, gentle grade
        "[0:v]scale=%(w)d:%(h)d:force_original_aspect_ratio=increase,"
        "crop=%(w)d:%(h)d,setsar=1,"
        "setpts=%(f).4f*PTS,"
        "minterpolate=fps=%(fps)d:mi_mode=mci:mc_mode=obmc:me_mode=bilat:me=hexbs:vsbmc=0,"
        "tpad=stop_mode=clone:stop_duration=2,"
        "eq=brightness=-0.03:contrast=1.04:saturation=1.05,"
        "vignette=PI/7,"
        "fade=t=in:st=0:d=1.0,fade=t=out:st=%(fo).2f:d=1.5[v];"
        # text: fades in, drifts up a little, fades out before the end
        "[1:v]format=rgba,"
        "fade=t=in:st=1.5:d=1.5:alpha=1,"
        "fade=t=out:st=%(to).2f:d=1.2:alpha=1[t];"
        "[v][t]overlay=x=0:y='14-5*min(t\\,3)':format=auto,format=yuv420p[out]"
    ) % {"w": W, "h": H, "f": factor, "fps": FPS,
         "fo": DURATION - 1.5, "to": DURATION - 3.0}

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-t", "%.3f" % used, "-i", bg_path,
        "-loop", "1", "-framerate", str(FPS), "-i", text_png,
        "-filter_complex", filter_complex,
        "-map", "[out]", "-an", "-t", str(DURATION),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-movflags", "+faststart",
        out_path,
    ]
    subprocess.run(cmd, check=True)


# --------------------------------------------------------------------------
# Telegram + backend report
# --------------------------------------------------------------------------

def scrub(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def send_to_telegram(token, chat_id, video_path, content_id, quote, caption, song):
    text = (
        "🥀 " + quote + "\n\n"
        "🖤 " + caption + "\n\n"
        "🎵 " + (song or "-") + "\n\n"
        + HASHTAGS + "\n\n"
        "🆔 " + content_id
    )[:1000]

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "❤️ Approve", "callback_data": "approve:" + content_id},
                {"text": "❌ Reject", "callback_data": "reject:" + content_id},
            ],
            [{"text": "🔄 Regenerate", "callback_data": "regen:" + content_id}],
        ]
    }

    with open(video_path, "rb") as f:
        r = requests.post(
            "https://api.telegram.org/bot%s/sendVideo" % token,
            data={
                "chat_id": chat_id,
                "caption": text,
                "supports_streaming": "true",
                "width": W,
                "height": H,
                "duration": DURATION,
                "reply_markup": json.dumps(keyboard),
            },
            files={"video": ("adhuro_katha.mp4", f, "video/mp4")},
            timeout=240,
        )

    data = r.json()
    if not data.get("ok"):
        raise RuntimeError("Telegram error: %s" % data.get("description"))
    return data["result"]["video"]["file_id"]


def report(url, secret, payload):
    body = dict(payload)
    body["render_secret"] = secret
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=60)
            if r.status_code < 400:
                return True
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def build_video(bg_url, quote, bg_path, text_png, out_path):
    print("Downloading background...")
    download(bg_url, bg_path)

    source_duration = probe_duration(bg_path)
    factor, used = plan_speed(source_duration)
    print("Source %.1fs -> slow-motion x%.2f (uses %.1fs)"
          % (source_duration, factor, used))

    layout = layout_text(quote)
    print("Text: size %d, %d lines" % (layout["size"], len(layout["lines"])))

    luma = sample_luma(bg_path, layout["bbox"], used)
    print("Brightness behind text: %.0f -> %s text"
          % (luma, "dark" if luma > 150 else "white"))
    make_text_png(layout, luma, text_png)

    print("Rendering video (this takes a few minutes)...")
    make_video(bg_path, text_png, out_path, factor, used)

    size = os.path.getsize(out_path)
    if size > 45_000_000:  # Telegram bots can send up to 50 MB
        print("File too big (%.1f MB), re-encoding smaller..." % (size / 1e6))
        make_video(bg_path, text_png, out_path, factor, used, crf=27)
    print("Video size: %.1f MB" % (os.path.getsize(out_path) / 1e6))


def main():
    e = os.environ.get
    content_id = e("CONTENT_ID", "")
    quote = e("QUOTE", "")
    caption = e("CAPTION", "")
    song = e("SONG", "")
    bg_url = e("BACKGROUND_URL", "")
    token = e("TELEGRAM_BOT_TOKEN", "")
    chat_id = e("OWNER_CHAT_ID", "")
    apps_url = e("APPS_SCRIPT_URL", "")
    secret = e("RENDER_SECRET", "")
    secrets = [token, secret, apps_url]

    try:
        for name, value in [("CONTENT_ID", content_id), ("QUOTE", quote),
                            ("BACKGROUND_URL", bg_url),
                            ("TELEGRAM_BOT_TOKEN", token),
                            ("OWNER_CHAT_ID", chat_id)]:
            if not value:
                raise RuntimeError("%s is missing" % name)

        os.makedirs(WORK, exist_ok=True)
        bg_path = os.path.join(WORK, "background.mp4")
        text_png = os.path.join(WORK, "text.png")
        out_path = os.path.join(WORK, "final.mp4")

        build_video(bg_url, quote, bg_path, text_png, out_path)

        print("Sending to Telegram...")
        file_id = send_to_telegram(
            token, chat_id, out_path, content_id, quote, caption, song
        )

        if apps_url and secret:
            ok = report(apps_url, secret, {
                "status": "done",
                "id": content_id,
                "file_id": file_id,
            })
            print("Backend notified:", ok)
        print("Done.")

    except Exception as exc:  # noqa: BLE001
        message = scrub("%s: %s" % (type(exc).__name__, exc), secrets)
        print("RENDER FAILED:", message)
        traceback.print_exc()
        if apps_url and secret:
            report(apps_url, secret, {
                "status": "failed",
                "id": content_id,
                "error": message[:400],
            })
        sys.exit(1)


if __name__ == "__main__":
    main()