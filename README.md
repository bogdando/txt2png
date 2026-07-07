txt2png
=======

Stream piped text (stdout) into 1568x1568 PNG frames and serve them over HTTP.

Designed for dense context transfer to vision-capable LLMs: each frame is a
monospace-rendered page at 20pt, the documented Claude vision maximum resolution,
with no upscale penalty.  Based on
[png_wrap.py](https://github.com/bogdando/opendev-agents/blob/master/src/rag_mcp/png_wrap.py)
rendering parameters.

Architecture
------------

```
┌────────────┐        ┌──────────────────┐                    ┌───────────────┐       ┌─────────────────┐
│            │        │                  │                    │               │       │   HTTP Server   │
│ stdin pipe ├──read──► Text Buffer      ├─chars_per_frame───► PNG Renderer   ├─store─► In-memory dict  │
│            │  lines │ (nl → space)     │   reached         │ (Pillow)      │ bytes │ {name: bytes}   │
└────────────┘        └──────────────────┘                    └───────┬───────┘       └────────┬────────┘
                                                                      │                       │
                                                                      │               127.0.0.1:42000
                                                              print URL│
                                                                      ▼
                                                              ┌───────────────┐
                                                              │    stdout     │
                                                              │ (URLs only)   │
                                                              └───────────────┘
```

Rendering parameters
--------------------

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Frame size | 1568x1568 | Claude vision maximum (no upscale penalty) |
| Margin | 20px | Keeps text off frame edges |
| Font size | 20pt | Balances density with reliable retrieval (22pt=100%, 16pt=17%) |
| Line spacing | 3px | Tight but readable |
| Font | Monospace with fallback chain | DejaVu Sans Mono → Liberation Mono → SourceCodePro → default bitmap |
| Background | White `(255,255,255)` | |
| Text color | Black `(0,0,0)` | |
| Footer | Gray `(128,128,128)`, centered | `— page N/M —` |
| PNG encoding | `optimize=True` | Smaller file size |

Adaptive calibration
--------------------

On startup, before reading any stdin:

1. Load the best available monospace font at 20pt
2. Measure `line_height` via `font.getbbox("Ag")`
3. Compute `wrap_width = usable_width / char_width` (monospace `"M"` bbox)
4. Compute `lines_per_frame = usable_height / line_height`
5. Derive `chars_per_frame = lines_per_frame * wrap_width`

These metrics adapt to whatever font is actually found on the system.
All subsequent frames use the same capacity.

Streaming pipeline
------------------

- Main thread reads `sys.stdin` line-by-line (responsive for pipes)
- Each line's trailing newline is replaced with a space, appended to a buffer
- When `len(buffer) >= chars_per_frame`: slice off one frame's worth, render PNG,
  store it, print `http://127.0.0.1:42000/frame_NNNN.png`
- After EOF: render any remaining buffer as a final (partial) frame
- After all frames emitted: patch footers with correct total page count,
  print summary to stderr, block on `signal.pause()` keeping the server alive

Usage
-----

```bash
some_command | python3 txt2png.py
```

Output (stdout):

```
http://127.0.0.1:42000/frame_0001.png
http://127.0.0.1:42000/frame_0002.png
...
```

The server stays alive until Ctrl+C.

Installation
------------

```bash
pip install .
```

Or install dependencies only:

```bash
pip install -r requirements.txt
```

After installation, the `txt2png` console entry point is also available:

```bash
some_command | txt2png
```

Development
-----------

Run tests with tox:

```bash
tox
```

Or run the test suite directly:

```bash
pip install -r test-requirements.txt
stestr run
```

Lint:

```bash
tox -e pep8
```

Project structure
-----------------

```
txt2png.py              Main script (executable)
pyproject.toml          Build config, dependencies, entry point
setup.cfg               Package metadata
setup.py                Setuptools shim
tox.ini                 Test environments (py312, pep8)
.stestr.conf            stestr test discovery
requirements.txt        Runtime dependencies (Pillow)
test-requirements.txt   Test dependencies (stestr)
tests/
  test_txt2png.py       Unit and integration tests
```

License
-------

Apache-2.0
