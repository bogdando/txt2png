#!/usr/bin/env python3
"""Stream stdin text into 1568x1568 PNG frames served over HTTP.

Reads piped stdout, replaces newlines with spaces, renders full frames
as PNG images using Pillow, serves them from disk via a persistent
background server on 127.0.0.1:42000, and prints only the frame URLs
to stdout.  Frame capacity re-adapts after every frame based on actual
text consumption, so word-boundary variations never cause overflow or
wasted space.

The server survives process exit.  On the next invocation, the existing
server is reused and frames are overwritten starting from 0001.
"""

from __future__ import annotations

import io
import os
import re
import select
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image, ImageDraw, ImageFont

FRAME_SIZE = 1568
MARGIN = 20
FONT_SIZE = 19
LINE_SPACING = 2
HOST = "127.0.0.1"
PORT = 42000
FRAME_DIR = os.environ.get("TXT2PNG_DIR", "/tmp/txt2png")
MAX_FRAMES = int(os.environ.get("TXT2PNG_MAX_FRAMES", "99"))

_USABLE = FRAME_SIZE - 2 * MARGIN

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    "/usr/share/fonts/adobe-source-code-pro/SourceCodePro-Regular.otf",
]


# CSI sequences (colors, cursor, erase) and OSC sequences (window title etc.)
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07')


def _clean_line(line: str) -> str:
    """Strip ANSI escape sequences and trailing whitespace from a line."""
    return _ANSI_RE.sub("", line).rstrip()


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
    # Reserve two chars width on the right to prevent clipping
    wrap_width = max(1, _USABLE // char_w - 2)
    # Reserve one line at the bottom for the page footer
    lines_per_frame = max(1, _USABLE // line_h - 1)
    return wrap_width, lines_per_frame, line_h


def _consume_frame(buf: str, wrap_width: int,
                   lines_per_frame: int) -> tuple[str, str]:
    """Wrap *buf* and extract exactly one frame's worth of lines.

    Returns ``(frame_text, remaining_buf)`` where *frame_text* contains
    newline-joined wrapped lines ready for rendering and *remaining_buf*
    is the unconsumed tail reconstructed from leftover wrapped lines plus
    any text beyond the working window.
    """
    window = lines_per_frame * (wrap_width + 1) * 2
    candidate = re.sub(r"  +", " ", buf[:window])
    tail = buf[window:]

    wrapped = textwrap.wrap(candidate, width=wrap_width, break_on_hyphens=False)
    frame_lines = wrapped[:lines_per_frame]
    rest_lines = wrapped[lines_per_frame:]

    frame_text = "\n".join(frame_lines)
    remaining = " ".join(rest_lines)
    if tail:
        remaining = (remaining + " " + tail) if remaining else tail

    return frame_text, remaining


def _render_frame(text: str, font, line_h: int, wrap_width: int,
                  lines_per_frame: int, page_num: int,
                  total_hint: str = "?") -> bytes:
    """Render a single frame's worth of text into a PNG byte buffer."""
    wrapped = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            wrapped.append("")
        else:
            wrapped.extend(textwrap.wrap(raw_line, width=wrap_width,
                                        break_on_hyphens=False))

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


def _store_frame(name: str, data: bytes) -> None:
    """Write a frame to disk atomically (temp-write + rename)."""
    os.makedirs(FRAME_DIR, exist_ok=True)
    fpath = os.path.join(FRAME_DIR, name)
    tmp = fpath + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, fpath)


def _load_frame(name: str) -> bytes | None:
    """Read a frame from disk, or return None if missing."""
    fpath = os.path.join(FRAME_DIR, name)
    try:
        with open(fpath, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.lstrip("/")
        data = _load_frame(name)
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
    """Start an HTTP server in a daemon thread."""
    server = HTTPServer((HOST, port), _Handler)
    server.allow_reuse_address = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def _is_server_running(port: int = PORT) -> bool:
    """Probe whether a server is already listening."""
    try:
        with socket.create_connection((HOST, port), timeout=1):
            return True
    except OSError:
        return False


def _ensure_server() -> None:
    """Reuse the existing server or spawn a detached one."""
    if _is_server_running():
        print("[txt2png] reusing existing server", file=sys.stderr)
        return
    os.makedirs(FRAME_DIR, exist_ok=True)
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if _is_server_running():
            print("[txt2png] server started", file=sys.stderr)
            return
        time.sleep(0.1)
    print("[txt2png] warning: server may not have started", file=sys.stderr)


def main() -> None:
    if sys.stdin.isatty():
        print("Usage: some_command | python3 txt2png.py", file=sys.stderr)
        raise SystemExit(1)

    font = _get_font()
    wrap_width, lines_per_frame, line_h = _calibrate(font)
    chars_per_frame = int(lines_per_frame * wrap_width * 1.15)

    print(
        f"[txt2png] font loaded, {wrap_width} cols × {lines_per_frame} rows "
        f"~{chars_per_frame} chars/frame (adaptive)",
        file=sys.stderr,
    )

    _ensure_server()
    base_url = f"http://{HOST}:{PORT}"

    buf = ""
    frame_num = 0

    def emit_frame(text: str) -> None:
        nonlocal frame_num
        frame_num += 1
        # Rotate back to 1 after MAX_FRAMES
        slot = ((frame_num - 1) % MAX_FRAMES) + 1
        name = f"frame_{slot:04d}.png"
        data = _render_frame(
            text, font, line_h, wrap_width, lines_per_frame,
            page_num=frame_num, total_hint="…",
        )
        _store_frame(name, data)
        url = f"{base_url}/{name}"
        print(url, flush=True)

    FLUSH_TIMEOUT = 15.0
    eof = False
    line_buf = ""
    stdin_fd = sys.stdin.fileno()

    while not eof:
        ready, _, _ = select.select([stdin_fd], [], [], FLUSH_TIMEOUT)
        if ready:
            raw = os.read(stdin_fd, 8192)
            if not raw:
                eof = True
            else:
                line_buf += raw.decode(errors="replace")
        else:
            # Timeout — flush whatever is in the buffer as a partial frame
            if buf.strip():
                frame_text, buf = _consume_frame(buf, wrap_width, lines_per_frame)
                if frame_text.strip():
                    emit_frame(frame_text)
            continue

        # Split on newlines, process complete lines
        while "\n" in line_buf:
            line, line_buf = line_buf.split("\n", 1)
            buf += _clean_line(line) + " "

        if eof and line_buf:
            buf += _clean_line(line_buf) + " "
            line_buf = ""

        while len(buf) >= chars_per_frame:
            buf_len = len(buf)
            frame_text, buf = _consume_frame(buf, wrap_width, lines_per_frame)
            if not frame_text.strip():
                break
            chars_per_frame = max(wrap_width, buf_len - len(buf))
            emit_frame(frame_text)

    # Drain remaining buffer
    while buf.strip():
        frame_text, buf = _consume_frame(buf, wrap_width, lines_per_frame)
        if not frame_text.strip():
            break
        emit_frame(frame_text)

    # Retroactively patch total page counts in stored frames
    total = frame_num
    patched: set[str] = set()
    for idx in range(1, total + 1):
        slot = ((idx - 1) % MAX_FRAMES) + 1
        name = f"frame_{slot:04d}.png"
        if name in patched:
            continue
        patched.add(name)
        old = _load_frame(name)
        if old is None:
            continue
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
        _store_frame(name, buf2.getvalue())

    print(
        f"[txt2png] {total} frame(s) at {base_url} "
        f"(max {MAX_FRAMES} on disk, server persists in background)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    if "--serve" in sys.argv:
        os.makedirs(FRAME_DIR, exist_ok=True)
        _start_server()
        signal.pause()
    else:
        main()
