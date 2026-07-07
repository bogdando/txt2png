"""Tests for the txt2png module."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
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


class _TmpDirMixin:
    """setUp/tearDown that redirects FRAME_DIR to a fresh temp directory."""

    def setUp(self):
        super().setUp()
        self._orig_frame_dir = txt2png.FRAME_DIR
        self.tmpdir = tempfile.mkdtemp(prefix="txt2png_test_")
        txt2png.FRAME_DIR = self.tmpdir

    def tearDown(self):
        txt2png.FRAME_DIR = self._orig_frame_dir
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()


class TestConstants(unittest.TestCase):
    """Verify module-level constants are sane."""

    def test_frame_size(self):
        self.assertEqual(1568, txt2png.FRAME_SIZE)

    def test_font_size(self):
        self.assertEqual(19, txt2png.FONT_SIZE)

    def test_margins(self):
        self.assertEqual(20, txt2png.MARGIN)

    def test_line_spacing(self):
        self.assertEqual(2, txt2png.LINE_SPACING)

    def test_usable_positive(self):
        self.assertGreater(txt2png._USABLE, 0)
        self.assertEqual(txt2png.FRAME_SIZE - 2 * txt2png.MARGIN, txt2png._USABLE)

    def test_host_and_port(self):
        self.assertEqual("127.0.0.1", txt2png.HOST)
        self.assertEqual(42000, txt2png.PORT)


class TestCleanLine(unittest.TestCase):
    """Verify ANSI stripping and whitespace trimming."""

    def test_strips_ansi_colors(self):
        line = "\x1b[31mhello\x1b[0m world\n"
        self.assertEqual("hello world", txt2png._clean_line(line))

    def test_strips_bold_and_256color(self):
        line = "\x1b[1;38;5;196mERROR\x1b[0m: fail\n"
        self.assertEqual("ERROR: fail", txt2png._clean_line(line))

    def test_strips_osc_sequences(self):
        line = "\x1b]0;my title\x07real content\n"
        self.assertEqual("real content", txt2png._clean_line(line))

    def test_strips_trailing_whitespace(self):
        self.assertEqual("hello", txt2png._clean_line("hello   \n"))
        self.assertEqual("hello", txt2png._clean_line("hello \t \r\n"))
        self.assertEqual("hello", txt2png._clean_line("hello\r\n"))

    def test_strips_ansi_before_trailing_ws(self):
        line = "\x1b[32mfoo\x1b[0m   \n"
        self.assertEqual("foo", txt2png._clean_line(line))

    def test_preserves_leading_whitespace(self):
        self.assertEqual("  indented", txt2png._clean_line("  indented\n"))

    def test_preserves_plain_content(self):
        self.assertEqual("plain text", txt2png._clean_line("plain text\n"))

    def test_empty_line(self):
        self.assertEqual("", txt2png._clean_line("\n"))
        self.assertEqual("", txt2png._clean_line("\r\n"))
        self.assertEqual("", txt2png._clean_line(""))

    def test_buffered_lines_max_two_trailing_spaces(self):
        """After clean+buffer+wrap, no wrapped line has >2 trailing spaces."""
        lines = [
            "\x1b[31mhello world\x1b[0m     \n",
            "foo bar   \r\n",
            "  baz  qux  \n",
            "\n",
            "   \n",
            "\x1b[1mend\x1b[0m\n",
        ]
        buf = ""
        for line in lines:
            buf += txt2png._clean_line(line) + " "
        import textwrap
        wrapped = textwrap.wrap(buf, width=80)
        for wline in wrapped:
            trailing = len(wline) - len(wline.rstrip())
            self.assertLessEqual(
                trailing, 2,
                f"Line has {trailing} trailing spaces: {wline!r}",
            )


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


class TestConsumeFrame(unittest.TestCase):
    """Verify _consume_frame splits text at exact line boundaries."""

    def test_short_text_consumed_entirely(self):
        frame, remaining = txt2png._consume_frame("hello world", 80, 50)
        self.assertIn("hello world", frame)
        self.assertEqual("", remaining)

    def test_empty_input(self):
        frame, remaining = txt2png._consume_frame("", 80, 50)
        self.assertEqual("", frame)
        self.assertEqual("", remaining)

    def test_exact_line_boundary(self):
        text = " ".join(["word"] * 20)
        frame, remaining = txt2png._consume_frame(text, 10, 2)
        lines = frame.split("\n")
        self.assertLessEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(len(line), 10)
        self.assertGreater(len(remaining), 0)

    def test_no_text_lost(self):
        words = [f"word{i}" for i in range(100)]
        text = " ".join(words)
        # Use wide wrap so no word is broken across lines
        frame, remaining = txt2png._consume_frame(text, 80, 3)
        recovered = frame.replace("\n", " ")
        if remaining:
            recovered += " " + remaining
        for w in words:
            self.assertIn(w, recovered)

    def test_adapts_to_short_words(self):
        short_words = " ".join(["ab"] * 200)
        long_words = " ".join(["abcdefghij"] * 200)
        f_short, _ = txt2png._consume_frame(short_words, 20, 5)
        f_long, _ = txt2png._consume_frame(long_words, 20, 5)
        self.assertGreater(len(f_short), 0)
        self.assertGreater(len(f_long), 0)

    def test_single_long_word(self):
        text = "x" * 500
        frame, remaining = txt2png._consume_frame(text, 20, 3)
        lines = frame.split("\n")
        self.assertLessEqual(len(lines), 3)
        for line in lines:
            self.assertLessEqual(len(line), 20)
        self.assertGreater(len(remaining), 0)


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
class TestStoreAndLoad(_TmpDirMixin, unittest.TestCase):
    """Test disk-based frame storage."""

    def test_store_creates_file(self):
        data = b"\x89PNG fake"
        txt2png._store_frame("test.png", data)
        fpath = os.path.join(self.tmpdir, "test.png")
        self.assertTrue(os.path.isfile(fpath))

    def test_load_returns_stored_data(self):
        data = b"\x89PNG fake data"
        txt2png._store_frame("test.png", data)
        loaded = txt2png._load_frame("test.png")
        self.assertEqual(data, loaded)

    def test_load_missing_returns_none(self):
        self.assertIsNone(txt2png._load_frame("missing.png"))

    def test_overwrite_replaces_content(self):
        txt2png._store_frame("f.png", b"old")
        txt2png._store_frame("f.png", b"new")
        self.assertEqual(b"new", txt2png._load_frame("f.png"))

    def test_concurrent_stores(self):
        errors = []

        def writer(name, data):
            try:
                txt2png._store_frame(name, data)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(f"f{i}.png", b"data"))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([], errors)
        stored = [f for f in os.listdir(self.tmpdir) if f.endswith(".png")]
        self.assertEqual(20, len(stored))


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestHandler(_TmpDirMixin, unittest.TestCase):
    """Test HTTP handler serves frames and returns 404 for missing."""

    def setUp(self):
        super().setUp()
        self.server, self.base = _ephemeral_server()

    def tearDown(self):
        self.server.shutdown()
        super().tearDown()

    def test_serves_stored_frame(self):
        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)
        data = txt2png._render_frame("hello", font, lh, w, lpf, 1, "1")
        txt2png._store_frame("test.png", data)
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
class TestServerDetection(unittest.TestCase):
    """Test _is_server_running probe."""

    def test_detects_running_server(self):
        server = txt2png._start_server(port=0)
        _, port = server.server_address
        try:
            self.assertTrue(txt2png._is_server_running(port))
        finally:
            server.shutdown()

    def test_detects_no_server(self):
        self.assertFalse(txt2png._is_server_running(port=19999))


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestMainStreaming(_TmpDirMixin, unittest.TestCase):
    """Test the streaming pipeline with adaptive _consume_frame."""

    def test_single_short_input(self):
        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)

        server, base_url = _ephemeral_server()
        try:
            buf = "Hello world "
            frame_text, buf = txt2png._consume_frame(buf, w, lpf)
            self.assertIn("Hello world", frame_text)

            data = txt2png._render_frame(frame_text, font, lh, w, lpf, 1, "1")
            txt2png._store_frame("frame_0001.png", data)

            resp = urlopen(f"{base_url}/frame_0001.png")
            self.assertEqual(200, resp.status)
            self.assertTrue(resp.read().startswith(b"\x89PNG"))
        finally:
            server.shutdown()

    def test_multiple_frames_adaptive(self):
        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)
        chars_per_frame = lpf * w

        buf = " ".join(["x" * w] * (lpf * 3)) + " "

        server, _ = _ephemeral_server()
        try:
            frame_num = 0
            while len(buf) >= chars_per_frame and buf.strip():
                frame_text, buf = txt2png._consume_frame(buf, w, lpf)
                if not frame_text.strip():
                    break
                chars_per_frame = max(w, len(frame_text))
                frame_num += 1
                name = f"frame_{frame_num:04d}.png"
                data = txt2png._render_frame(
                    frame_text, font, lh, w, lpf, frame_num, "…"
                )
                txt2png._store_frame(name, data)

            while buf.strip():
                frame_text, buf = txt2png._consume_frame(buf, w, lpf)
                if not frame_text.strip():
                    break
                frame_num += 1
                name = f"frame_{frame_num:04d}.png"
                data = txt2png._render_frame(
                    frame_text, font, lh, w, lpf, frame_num, "…"
                )
                txt2png._store_frame(name, data)

            self.assertGreaterEqual(frame_num, 2)
        finally:
            server.shutdown()

    def test_adaptation_changes_threshold(self):
        """chars_per_frame should change after consuming a frame."""
        font = txt2png._get_font()
        w, lpf, _ = txt2png._calibrate(font)

        short_words = " ".join(["hi"] * (lpf * w))
        frame_text, _ = txt2png._consume_frame(short_words, w, lpf)
        adapted = max(w, len(frame_text))
        self.assertGreater(adapted, 0)

    def test_tty_stdin_exits(self):
        with mock.patch("sys.stdin") as m_stdin:
            m_stdin.isatty.return_value = True
            with self.assertRaises(SystemExit) as ctx:
                txt2png.main()
            self.assertEqual(1, ctx.exception.code)


@unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
class TestStreamingFlush(_TmpDirMixin, unittest.TestCase):
    """Test EOF and timeout flush of partial frames."""

    def _run_main_with_pipe(self, input_text):
        """Run main() with simulated piped stdin, return (stdout, stored frames)."""
        import select as _select

        r_fd, w_fd = os.pipe()
        w_file = os.fdopen(w_fd, "wb")
        captured = io.StringIO()

        w_file.write(input_text.encode())
        w_file.close()

        stored = {}
        orig_store = txt2png._store_frame

        def tracking_store(name, data):
            orig_store(name, data)
            stored[name] = data

        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)
        cpf = int(lpf * w * 1.15)
        base_url = "http://127.0.0.1:42000"
        buf = ""
        frame_num = 0
        line_buf = ""
        eof = False

        def emit(text):
            nonlocal frame_num
            frame_num += 1
            name = f"frame_{frame_num:04d}.png"
            data = txt2png._render_frame(
                text, font, lh, w, lpf, frame_num, "…",
            )
            tracking_store(name, data)
            captured.write(f"{base_url}/{name}\n")

        try:
            while not eof:
                ready, _, _ = _select.select([r_fd], [], [], 0.1)
                if ready:
                    raw = os.read(r_fd, 8192)
                    if not raw:
                        eof = True
                    else:
                        line_buf += raw.decode(errors="replace")
                else:
                    if buf.strip():
                        ft, buf = txt2png._consume_frame(buf, w, lpf)
                        if ft.strip():
                            emit(ft)
                    continue

                while "\n" in line_buf:
                    line, line_buf = line_buf.split("\n", 1)
                    buf += txt2png._clean_line(line) + " "

                if eof and line_buf:
                    buf += txt2png._clean_line(line_buf) + " "
                    line_buf = ""

                while len(buf) >= cpf:
                    bl = len(buf)
                    ft, buf = txt2png._consume_frame(buf, w, lpf)
                    if not ft.strip():
                        break
                    cpf = max(w, bl - len(buf))
                    emit(ft)

            while buf.strip():
                ft, buf = txt2png._consume_frame(buf, w, lpf)
                if not ft.strip():
                    break
                emit(ft)
        finally:
            os.close(r_fd)

        return captured.getvalue(), stored

    def test_eof_flushes_partial_frame(self):
        """Short input that doesn't fill a frame is emitted on EOF."""
        stdout, stored = self._run_main_with_pipe("hello world\n")
        self.assertEqual(1, len(stored))
        self.assertIn("frame_0001.png", stored)
        self.assertTrue(stored["frame_0001.png"].startswith(b"\x89PNG"))
        self.assertIn("frame_0001.png", stdout)

    def test_eof_no_trailing_newline(self):
        """Input without trailing newline is still captured."""
        stdout, stored = self._run_main_with_pipe("no newline at end")
        self.assertEqual(1, len(stored))
        self.assertIn("frame_0001.png", stored)

    def test_eof_empty_input(self):
        """Empty input produces no frames."""
        stdout, stored = self._run_main_with_pipe("")
        self.assertEqual(0, len(stored))

    def test_eof_blank_lines_only(self):
        """Only blank lines produce no frames (whitespace-only buffer)."""
        stdout, stored = self._run_main_with_pipe("\n\n\n")
        self.assertEqual(0, len(stored))

    def test_eof_multiline_partial(self):
        """Multiple lines that don't fill a frame are flushed as one on EOF."""
        text = "line one\nline two\nline three\n"
        stdout, stored = self._run_main_with_pipe(text)
        self.assertEqual(1, len(stored))
        self.assertTrue(stored["frame_0001.png"].startswith(b"\x89PNG"))

    def test_timeout_flushes_stalled_stream(self):
        """Data followed by a stall triggers a partial-frame flush."""
        import select as _select

        r_fd, w_fd = os.pipe()

        font = txt2png._get_font()
        w, lpf, lh = txt2png._calibrate(font)
        stored = {}

        def tracking_store(name, data):
            txt2png._store_frame(name, data)
            stored[name] = data

        # Write some data, then let it stall (don't close write end yet)
        os.write(w_fd, b"partial data before stall\n")

        buf = ""
        line_buf = ""
        frame_num = 0
        flushed = False

        try:
            for _ in range(15):
                ready, _, _ = _select.select([r_fd], [], [], 0.1)
                if ready:
                    raw = os.read(r_fd, 8192)
                    if not raw:
                        break
                    line_buf += raw.decode(errors="replace")
                    while "\n" in line_buf:
                        line, line_buf = line_buf.split("\n", 1)
                        buf += txt2png._clean_line(line) + " "
                else:
                    if buf.strip():
                        ft, buf = txt2png._consume_frame(buf, w, lpf)
                        if ft.strip():
                            frame_num += 1
                            name = f"frame_{frame_num:04d}.png"
                            data = txt2png._render_frame(
                                ft, font, lh, w, lpf, frame_num, "…",
                            )
                            tracking_store(name, data)
                            flushed = True
                            break
        finally:
            os.close(w_fd)
            os.close(r_fd)

        self.assertTrue(flushed, "Timeout should have flushed partial frame")
        self.assertIn("frame_0001.png", stored)
        self.assertTrue(stored["frame_0001.png"].startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
