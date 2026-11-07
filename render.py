#!/usr/bin/env python3
"""Adhuro Katha renderer.

Background video + Nepali quote -> 1080x1920 mp4 -> Telegram (with approval
buttons) -> report back to the Apps Script backend.

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
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

W, H = 1080, 1920
FPS = 30
DURATION = 10  # seconds
WORK = "work"
HASHTAGS = "#अधुरोकथा #Loneliness #NepaliSadQuotes #DeepFeelings #BrokenHeart"
UA = {"User-Agent": "Mozilla/5.0 (AdhuroKathaRenderer)"}


# --------------------------------------------------------------------------
# Text image
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


def make_text_png(quote, out_path):
    """Draws the quote on a transparent 1080x1920 image."""
    quote = quote.strip().strip('"“”').strip()

    font_path = find_font()
    max_width = W - 220  # generous side margins

    chosen = None
    for size in range(92, 47, -4):
        font = load_font(font_path, size)
        lines = wrap_lines(quote, font, max_width)
        line_h = int(size * 1.65)
        total_h = line_h * len(lines)
        if len(lines) <= 8 and total_h <= 900:
            chosen = (font, lines, line_h, total_h)
            break
    if chosen is None:  # very long quote: use the smallest size anyway
        font = load_font(font_path, 48)
        lines = wrap_lines(quote, font, max_width)
        line_h = int(48 * 1.65)
        chosen = (font, lines, line_h, line_h * len(lines))

    font, lines, line_h, total_h = chosen
    top = int(H * 0.46 - total_h / 2)

    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    text_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    tdraw = ImageDraw.Draw(text_layer)

    y = top
    for line in lines:
        x = (W - font.getlength(line)) / 2
        sdraw.text((x + 3, y + 5), line, font=font, fill=(0, 0, 0, 200))
        tdraw.text((x, y), line, font=font, fill=(255, 255, 255, 245))
        y += line_h

    # thin decorative lines above and below the quote
    for ly in (top - 70, top + total_h + 40):
        sdraw.rectangle([W // 2 - 60, ly, W // 2 + 60, ly + 2],
                        fill=(0, 0, 0, 160))
        tdraw.rectangle([W // 2 - 60, ly, W // 2 + 60, ly + 2],
                        fill=(255, 255, 255, 170))

    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    final = Image.alpha_composite(shadow, text_layer)
    final.save(out_path)
    return len(lines)


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


def make_video(bg_path, text_png, out_path, duration=DURATION):
    fade_out_at = duration - 1.0
    text_out_at = duration - 1.8

    filter_complex = (
        "[0:v]scale=%d:%d:force_original_aspect_ratio=increase,"
        "crop=%d:%d,setsar=1,fps=%d,"
        "eq=brightness=-0.12:contrast=1.06:saturation=0.85,"
        "vignette=PI/5,"
        "fade=t=in:st=0:d=0.8,fade=t=out:st=%.2f:d=1.0[v];"
        "[1:v]format=rgba,"
        "fade=t=in:st=1.0:d=1.2:alpha=1,"
        "fade=t=out:st=%.2f:d=1.0:alpha=1[t];"
        "[v][t]overlay=0:0:format=auto,format=yuv420p[out]"
    ) % (W, H, W, H, FPS, fade_out_at, text_out_at)

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", "-1", "-i", bg_path,
        "-loop", "1", "-framerate", str(FPS), "-i", text_png,
        "-t", str(duration),
        "-filter_complex", filter_complex,
        "-map", "[out]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
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
            timeout=180,
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

        print("Downloading background...")
        download(bg_url, bg_path)

        print("Drawing text...")
        n = make_text_png(quote, text_png)
        print("Quote lines:", n)

        print("Rendering video...")
        make_video(bg_path, text_png, out_path)
        print("Video size: %.1f MB" % (os.path.getsize(out_path) / 1e6))

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