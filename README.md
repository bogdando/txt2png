txt2png
=======

Stream piped text (stdout) into 1568x1568 PNG frames and serve them over HTTP.

Designed for dense context transfer to vision-capable LLMs: each frame is a
monospace-rendered page at 19pt, the documented Claude vision maximum resolution,
with no upscale penalty. 

Architecture
------------

```
┌────────────┐        ┌──────────────────┐                    ┌───────────────┐       ┌─────────────────┐
│            │        │                  │  _consume_frame    │               │       │  Persistent     │
│ stdin pipe ├──read──► Text Buffer      ├─(wrap+slice N ln)──► PNG Renderer  ├─disk──► HTTP Server     │
│            │  lines │ (nl → space)     │                    │ (Pillow)      │ write │ 127.0.0.1:42000 │
└────────────┘        └──────────────────┘                    └───────┬───────┘       └────────┬────────┘
                               ▲                                      │                       │
                               │ re-adapt chars_per_frame             │            serves from FRAME_DIR
                               │ from previous frame len      print URL│            (/tmp/txt2png)
                               └──────────────────────────────────────┘
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
| Font size | 19pt | Balances density with reliable retrieval (22pt=100%, 16pt=17%) |
| Line spacing | 2px | Tight but readable |
| Font | Monospace with fallback chain | DejaVu Sans Mono → Liberation Mono → SourceCodePro → default bitmap |
| Background | White `(255,255,255)` | |
| Text color | Black `(0,0,0)` | |
| Footer | Gray `(128,128,128)`, centered | `— page N/M —` |
| PNG encoding | `optimize=True` | Smaller file size |

Per-frame adaptive calibration
-------------------------------

On startup, font metrics are measured once to produce an initial estimate:

1. Load the best available monospace font at 20pt
2. Measure `line_height` via `font.getbbox("Ag")`
3. Compute `wrap_width = usable_width / char_width` (monospace `"M"` bbox)
4. Compute `lines_per_frame = usable_height / line_height`
5. Derive initial `chars_per_frame = lines_per_frame * wrap_width`

After each frame, `chars_per_frame` is re-adapted to the actual character
count that filled the previous frame.  This compensates for word-boundary
variations in `textwrap.wrap` — short words pack more characters per frame
than long words, and the threshold tracks this automatically.

The `_consume_frame()` function wraps a generous window of the buffer,
takes exactly `lines_per_frame` wrapped lines, and returns the remainder.
No text is ever clipped or lost to overflow.

Persistent server
-----------------

The HTTP server runs as a detached background process that survives the
main process exiting.  Frames are stored as PNG files on disk under
`FRAME_DIR` (default `/tmp/txt2png`, override with `TXT2PNG_DIR` env var).

On each invocation:

1. Probe `127.0.0.1:42000` — if a server is already listening, reuse it
2. Otherwise spawn a new detached server via `txt2png.py --serve`
3. Write frames to disk starting from `frame_0001.png`, overwriting any
   previous frames as it goes
4. After `TXT2PNG_MAX_FRAMES` (default 99), frame slots rotate back to
   `frame_0001.png`
5. Print URLs to stdout and exit — frames and server remain available

To stop the background server:

```bash
kill $(lsof -ti :42000)
```

Streaming pipeline
------------------

- Main thread reads `sys.stdin` via `select` + `os.read` (non-blocking)
- ANSI escape sequences are stripped; trailing whitespace is trimmed;
  consecutive spaces are collapsed
- When `len(buffer) >= chars_per_frame`:
  - `_consume_frame()` wraps the buffer and extracts exactly `lines_per_frame` lines
  - The frame is rendered to PNG, written to `FRAME_DIR`, and its URL is printed
  - `chars_per_frame` is re-adapted from actual buffer consumption
- If no data arrives for 5 seconds, any buffered text is flushed as a partial frame
- After EOF: drain any remaining buffer through `_consume_frame()` in a loop
- After all frames emitted: patch footers with correct total page count,
  print summary to stderr, exit (server persists)
- Frame slots rotate: frame 100 overwrites slot 1, 101 overwrites slot 2, etc.

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

The process exits after consuming stdin.  The server and frame files
persist in the background — run the command again and frames are
overwritten starting from `0001`.

Environment variables:

```bash
TXT2PNG_DIR=/my/frames some_command | python3 txt2png.py
TXT2PNG_MAX_FRAMES=50 some_command | python3 txt2png.py
```

| Variable | Default | Description |
|----------|---------|-------------|
| `TXT2PNG_DIR` | `/tmp/txt2png` | Directory for frame PNG files |
| `TXT2PNG_MAX_FRAMES` | `99` | Max frame slots on disk; after this, slots rotate back to 1 |

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
