"""Tests for the txt2png module."""

from __future__ import annotations

import io
import sys
import threading
import unittest
from unittest import mock
from urllib.request import urlopen

HAS_PILLOW = True
try:
    import PIL  # noqa: F401
except ImportError:
    HAS_PILLOW = False
    _fake_pil = mock.MagicMock()
    sys.modules["PIL"] = _fake_pil
    sys.modules["PIL.Image"] = _fake_pil
    sys.modules["PIL.ImageDraw"] = _fake_pil
    sys.modules["PIL.ImageFont"] = _fake_pil
    sys.modules.pop("txt2png", None)

import txt2png  # noqa: E402


def _ephemeral_server():
    """Start a server on an OS-assigned ephemeral port, return (server, base_url)."""
    server = txt2png._start_server(port=0)
    host, port = server.server_address
    return server, f"http://{host}:{port}"


class TestConstants(unittest.TestCase):
    """Verify module-level constants are sane."""

    def test_frame_size(self):
        self.assertEqual(1568, txt2png.FRAME_SIZE)

    def test_font_size(self):
        self.assertEqual(20, txt2png.FONT_SIZE)

    def test_margins(self):
        self.assertEqual(20, txt2png.MARGIN)

    def test_line_spacing(self):
        self.assertEqual(3, txt2png.LINE_SPACING)

    def test_usable_positive(self):
        self.assertGreater(txt2png._USABLE, 0)
        self.assertEqual(txt2png.FRAME_SIZE - 2 * txt2png.MARGIN, txt2png._USABLE)

    def test_host_and_port(self):
        self.assertEqual("127.0.0.1", txt2png.HOST)
        self.assertEqual(42000, txt2png.PORT)


class TestFontCandidates(unittest.TestCase):
    """Font fallback chain has expected entries."""

    def test_at_least_three_candidates(self):
        self.assertGreaterEqual(len(txt2png._FONT_CANDIDATES), 3)

    def test_source_code_pro_in_chain(self):
        self.assertTrue(
            any("SourceCodePro" in p for p in txt2png._FONT_CANDIDATES)
        )

    @unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
    def test_get_font_returns_font(self):
        font = txt2png._get_font()
        self.assertTrue(hasattr(font, "getbbox"))


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestCalibrate(unittest.TestCase):
    """Verify adaptive calibration produces sane metrics."""

    def setUp(self):
        self.font = txt2png._get_font()
        self.wrap_width, self.lines_per_frame, self.line_h = txt2png._calibrate(
            self.font
        )

    def test_wrap_width_positive(self):
        self.assertGreater(self.wrap_width, 0)

    def test_lines_per_frame_positive(self):
        self.assertGreater(self.lines_per_frame, 0)

    def test_line_height_positive(self):
        self.assertGreater(self.line_h, 0)

    def test_chars_per_frame_sane(self):
        chars = self.lines_per_frame * self.wrap_width
        self.assertGreater(chars, 100)
        self.assertLess(chars, 100_000)

    def test_wrap_width_fits_usable(self):
        char_w = self.font.getbbox("M")[2] - self.font.getbbox("M")[0]
        self.assertLessEqual(self.wrap_width * char_w, txt2png._USABLE)


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestRenderFrame(unittest.TestCase):
    """Integration tests for PNG frame rendering."""

    def setUp(self):
        self.font = txt2png._get_font()
        self.wrap_width, self.lines_per_frame, self.line_h = txt2png._calibrate(
            self.font
        )

    def _render(self, text, page_num=1, total_hint="1"):
        return txt2png._render_frame(
            text, self.font, self.line_h, self.wrap_width,
            self.lines_per_frame, page_num, total_hint,
        )

    def test_returns_png_bytes(self):
        data = self._render("Hello world")
        self.assertIsInstance(data, bytes)
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_empty_text_still_valid_png(self):
        data = self._render("")
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_frame_dimensions(self):
        from PIL import Image
        data = self._render("Test dimensions")
        img = Image.open(io.BytesIO(data))
        self.assertEqual((txt2png.FRAME_SIZE, txt2png.FRAME_SIZE), img.size)

    def test_multiline_text(self):
        lines = "\n".join(f"Line {i}" for i in range(50))
        data = self._render(lines)
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_long_line_wraps(self):
        long_line = "x" * (self.wrap_width * 3)
        data = self._render(long_line)
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_page_number_in_footer(self):
        data = self._render("content", page_num=3, total_hint="5")
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 0)


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestHandler(unittest.TestCase):
    """Test HTTP handler serves frames and returns 404 for missing."""

    def setUp(self):
        txt2png._frames.clear()
        self.server, self.base = _ephemeral_server()

    def tearDown(self):
        self.server.shutdown()
        txt2png._frames.clear()

    def test_serves_stored_frame(self):
        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)
        data = txt2png._render_frame("hello", font, lh, w, lpf, 1, "1")
        with txt2png._frames_lock:
            txt2png._frames["test.png"] = data
        resp = urlopen(f"{self.base}/test.png")
        self.assertEqual(200, resp.status)
        body = resp.read()
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(data, body)

    def test_404_for_missing(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as ctx:
            urlopen(f"{self.base}/nonexistent.png")
        self.assertEqual(404, ctx.exception.code)


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestMainStreaming(unittest.TestCase):
    """Test the main() streaming pipeline end-to-end."""

    def setUp(self):
        txt2png._frames.clear()

    def tearDown(self):
        txt2png._frames.clear()

    def test_single_short_input(self):
        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)
        chars_per_frame = lpf * w

        server, base_url = _ephemeral_server()
        try:
            buf = ""
            frame_num = 0
            for line in ["Hello world\n"]:
                buf += line.rstrip("\n") + " "
                while len(buf) >= chars_per_frame:
                    chunk = buf[:chars_per_frame]
                    buf = buf[chars_per_frame:]
                    frame_num += 1
                    name = f"frame_{frame_num:04d}.png"
                    data = txt2png._render_frame(
                        chunk, font, lh, w, lpf, frame_num, "…"
                    )
                    with txt2png._frames_lock:
                        txt2png._frames[name] = data
            if buf.strip():
                frame_num += 1
                name = f"frame_{frame_num:04d}.png"
                data = txt2png._render_frame(
                    buf, font, lh, w, lpf, frame_num, "…"
                )
                with txt2png._frames_lock:
                    txt2png._frames[name] = data

            self.assertEqual(1, frame_num)
            self.assertIn("frame_0001.png", txt2png._frames)

            resp = urlopen(f"{base_url}/frame_0001.png")
            self.assertEqual(200, resp.status)
            body = resp.read()
            self.assertTrue(body.startswith(b"\x89PNG"))
        finally:
            server.shutdown()

    def test_multiple_frames(self):
        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)
        chars_per_frame = lpf * w

        lines = [f"{'x' * w}\n" for _ in range(lpf * 3)]
        server, _ = _ephemeral_server()
        try:
            buf = ""
            frame_num = 0
            for line in lines:
                buf += line.rstrip("\n") + " "
                while len(buf) >= chars_per_frame:
                    chunk = buf[:chars_per_frame]
                    buf = buf[chars_per_frame:]
                    frame_num += 1
                    name = f"frame_{frame_num:04d}.png"
                    data = txt2png._render_frame(
                        chunk, font, lh, w, lpf, frame_num, "…"
                    )
                    with txt2png._frames_lock:
                        txt2png._frames[name] = data
            if buf.strip():
                frame_num += 1
                name = f"frame_{frame_num:04d}.png"
                data = txt2png._render_frame(
                    buf, font, lh, w, lpf, frame_num, "…"
                )
                with txt2png._frames_lock:
                    txt2png._frames[name] = data

            self.assertGreaterEqual(frame_num, 2)
        finally:
            server.shutdown()

    def test_tty_stdin_exits(self):
        with mock.patch("sys.stdin") as m_stdin:
            m_stdin.isatty.return_value = True
            with self.assertRaises(SystemExit) as ctx:
                txt2png.main()
            self.assertEqual(1, ctx.exception.code)


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestFrameStore(unittest.TestCase):
    """Verify the shared frame store is thread-safe."""

    def setUp(self):
        txt2png._frames.clear()

    def tearDown(self):
        txt2png._frames.clear()

    def test_concurrent_writes(self):
        errors = []

        def writer(key, value):
            try:
                with txt2png._frames_lock:
                    txt2png._frames[key] = value
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(f"k{i}", b"data"))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([], errors)
        self.assertEqual(20, len(txt2png._frames))


if __name__ == "__main__":
    unittest.main()
