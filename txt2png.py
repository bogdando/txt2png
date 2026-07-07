#!/usr/bin/env python3
"""Stream stdin text into 1568x1568 PNG frames served over HTTP.

Reads piped stdout, replaces newlines with spaces, renders full frames
as PNG images using Pillow, serves them on 127.0.0.1:42000, and prints
only the frame URLs to stdout.  The frame capacity is calibrated once
from the first available monospace font so layout adapts automatically.
"""

from __future__ import annotations

import io
import signal
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image, ImageDraw, ImageFont

FRAME_SIZE = 1568
MARGIN = 20
FONT_SIZE = 20
LINE_SPACING = 3
HOST = "127.0.0.1"
PORT = 42000

_USABLE = FRAME_SIZE - 2 * MARGIN

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    "/usr/share/fonts/adobe-source-code-pro/SourceCodePro-Regular.otf",
]

# Shared frame store: frame name -> PNG bytes
_frames: dict[str, bytes] = {}
_frames_lock = threading.Lock()


def _get_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, FONT_SIZE)
        except OSError:
            continue
    return ImageFont.load_default()


def _calibrate(font):
    """Return (wrap_width, lines_per_frame, line_height) for the loaded font."""
    bbox = font.getbbox("Ag")
    line_h = (bbox[3] - bbox[1]) + LINE_SPACING
    char_w = font.getbbox("M")[2] - font.getbbox("M")[0]
    wrap_width = max(1, _USABLE // char_w)
    lines_per_frame = max(1, _USABLE // line_h)
    return wrap_width, lines_per_frame, line_h


def _render_frame(text: str, font, line_h: int, wrap_width: int,
                  lines_per_frame: int, page_num: int,
                  total_hint: str = "?") -> bytes:
    """Render a single frame's worth of text into a PNG byte buffer."""
    wrapped = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            wrapped.append("")
        else:
            wrapped.extend(textwrap.wrap(raw_line, width=wrap_width))

    img = Image.new("RGB", (FRAME_SIZE, FRAME_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    y = MARGIN
    for line in wrapped[:lines_per_frame]:
        draw.text((MARGIN, y), line, fill=(0, 0, 0), font=font)
        y += line_h

    footer = f"— page {page_num}/{total_hint} —"
    fw = draw.textlength(footer, font=font)
    draw.text(
        ((FRAME_SIZE - fw) / 2, FRAME_SIZE - MARGIN - line_h),
        footer,
        fill=(128, 128, 128),
        font=font,
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.lstrip("/")
        with _frames_lock:
            data = _frames.get(name)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def _start_server(port: int = PORT) -> HTTPServer:
    server = HTTPServer((HOST, port), _Handler)
    server.allow_reuse_address = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def main() -> None:
    if sys.stdin.isatty():
        print("Usage: some_command | python3 txt2png.py", file=sys.stderr)
        raise SystemExit(1)

    font = _get_font()
    wrap_width, lines_per_frame, line_h = _calibrate(font)
    chars_per_frame = lines_per_frame * wrap_width

    print(
        f"[txt2png] font loaded, {wrap_width} cols × {lines_per_frame} rows "
        f"= {chars_per_frame} chars/frame",
        file=sys.stderr,
    )

    _start_server()
    base_url = f"http://{HOST}:{PORT}"

    buf = ""
    frame_num = 0

    def emit_frame(text: str) -> None:
        nonlocal frame_num
        frame_num += 1
        name = f"frame_{frame_num:04d}.png"
        data = _render_frame(
            text, font, line_h, wrap_width, lines_per_frame,
            page_num=frame_num, total_hint="…",
        )
        with _frames_lock:
            _frames[name] = data
        url = f"{base_url}/{name}"
        print(url, flush=True)

    for line in sys.stdin:
        buf += line.rstrip("\n") + " "
        while len(buf) >= chars_per_frame:
            chunk = buf[:chars_per_frame]
            buf = buf[chars_per_frame:]
            emit_frame(chunk)

    if buf.strip():
        emit_frame(buf)

    # Retroactively patch total page counts in all frames
    total = frame_num
    for idx in range(1, total + 1):
        name = f"frame_{idx:04d}.png"
        with _frames_lock:
            old = _frames.get(name)
        if old is None:
            continue
        # Re-render is the simplest way to fix the footer
        # Recover text from the buffer isn't possible, so we patch in-place
        # by overlaying just the footer area
        img = Image.open(io.BytesIO(old))
        draw = ImageDraw.Draw(img)
        footer_y = FRAME_SIZE - MARGIN - int(line_h)
        draw.rectangle(
            [0, footer_y, FRAME_SIZE, FRAME_SIZE],
            fill=(255, 255, 255),
        )
        footer = f"— page {idx}/{total} —"
        fw = draw.textlength(footer, font=font)
        draw.text(
            ((FRAME_SIZE - fw) / 2, footer_y),
            footer,
            fill=(128, 128, 128),
            font=font,
        )
        buf2 = io.BytesIO()
        img.save(buf2, format="PNG", optimize=True)
        with _frames_lock:
            _frames[name] = buf2.getvalue()

    print(
        f"\n[txt2png] {total} frame(s) served at {base_url}  "
        f"(Ctrl+C to stop)",
        file=sys.stderr,
    )

    try:
        signal.pause()
    except AttributeError:
        # Windows fallback
        threading.Event().wait()


if __name__ == "__main__":
    main()
