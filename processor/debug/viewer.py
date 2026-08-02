"""Interactive debug window.

Runs on the main thread (a hard requirement for GUI on macOS, and good
practice on Linux) while the pipeline runs in the background, so a slow
redraw or a dragged window never stalls the video output.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from processor.app import Processor
from processor.utils.logging import get_logger

log = get_logger(__name__)

#: key -> (stage name, label)
STAGE_KEYS = {
    "m": "movement",
    "b": "boundary",
    "p": "perspective",
    "c": "crop",
    "k": "blackbars",
    "f": "reflection",
    "l": "color",
    "z": "resize",
}

KEY_HELP = [
    ("0-9", "select view"),
    ("[ ]", "previous / next view"),
    ("g", "grid of all views"),
    ("o", "toggle info overlay"),
    ("h", "toggle this help"),
    ("space", "pause / resume redraw"),
    ("r", "force TV recalibration"),
    ("s", "save a snapshot"),
    ("w", "write config to disk"),
    ("q / esc", "quit"),
    ("", ""),
    ("m b p c", "toggle movement / boundary / perspective / crop"),
    ("k f l z", "toggle blackbars / reflection / color / resize"),
]

_FONT = cv2.FONT_HERSHEY_SIMPLEX


def _label(image: np.ndarray, text: str, colour=(255, 255, 255)) -> np.ndarray:
    cv2.rectangle(image, (0, 0), (image.shape[1], 22), (0, 0, 0), -1)
    cv2.putText(image, text, (8, 16), _FONT, 0.45, colour, 1, cv2.LINE_AA)
    return image


def build_grid(views: dict[str, np.ndarray], cell_width: int = 400) -> np.ndarray:
    """Tile every view into one image."""
    if not views:
        return np.zeros((240, 320, 3), dtype=np.uint8)

    cells: list[np.ndarray] = []
    cell_height = int(cell_width * 9 / 16)
    for name, image in views.items():
        if image is None or image.size == 0:
            continue
        canvas = np.zeros((cell_height, cell_width, 3), dtype=np.uint8)
        scale = min(cell_width / image.shape[1], cell_height / image.shape[0])
        resized = cv2.resize(
            image,
            (max(1, int(image.shape[1] * scale)), max(1, int(image.shape[0] * scale))),
            interpolation=cv2.INTER_AREA,
        )
        y0 = (cell_height - resized.shape[0]) // 2
        x0 = (cell_width - resized.shape[1]) // 2
        canvas[y0 : y0 + resized.shape[0], x0 : x0 + resized.shape[1]] = resized
        cells.append(_label(canvas, name))

    columns = 3 if len(cells) > 4 else 2
    rows: list[np.ndarray] = []
    for i in range(0, len(cells), columns):
        row = cells[i : i + columns]
        while len(row) < columns:
            row.append(np.zeros_like(cells[0]))
        rows.append(np.hstack(row))
    return np.vstack(rows)


class DebugViewer:
    def __init__(self, processor: Processor):
        self.processor = processor
        self.config = processor.config.debug
        self.view = self.config.view or "output"
        self.show_overlay = self.config.overlay
        self.show_help = False
        self.paused = False
        self._closed = False
        self._snapshot_dir = Path(self.config.snapshot_dir)

    # -- main loop ---------------------------------------------------------

    def run(self, fps: float = 25.0) -> None:
        self.processor.want_debug_views = True
        window = self.config.window_name
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        interval = 1.0 / fps if fps > 0 else 0.02
        log.info("Debug window open -- press 'h' for keyboard shortcuts")

        try:
            while self.processor.running and not self._closed:
                start = time.monotonic()
                if not self.paused:
                    canvas = self.render()
                    if canvas is not None:
                        cv2.imshow(window, canvas)

                key = cv2.waitKey(max(1, int((interval - (time.monotonic() - start)) * 1000)))
                if key != -1 and not self.handle_key(key & 0xFF):
                    break

                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            self.processor.want_debug_views = False
            cv2.destroyAllWindows()
            # Some OpenCV builds need a few more event-loop turns to actually
            # tear the window down.
            for _ in range(4):
                cv2.waitKey(1)

    # -- rendering ---------------------------------------------------------

    def _views(self) -> dict[str, np.ndarray]:
        ctx = self.processor.last_context
        if ctx is None:
            return {}
        views = self.processor.pipeline.debug_views(ctx)
        views.setdefault("source", ctx.source)
        return views

    def render(self) -> np.ndarray | None:
        views = self._views()
        if not views:
            return self._waiting_frame()

        if self.view == "grid":
            canvas = build_grid(views)
        else:
            image = views.get(self.view)
            if image is None:
                image = views.get("output")
            if image is None:
                return self._waiting_frame()
            canvas = image.copy()

        scale = self.config.scale
        if scale and scale != 1.0:
            canvas = cv2.resize(
                canvas,
                (int(canvas.shape[1] * scale), int(canvas.shape[0] * scale)),
                interpolation=cv2.INTER_NEAREST if scale > 1 else cv2.INTER_AREA,
            )

        if self.show_overlay:
            self._draw_overlay(canvas)
        if self.show_help:
            self._draw_help(canvas)
        return canvas

    def _waiting_frame(self) -> np.ndarray:
        canvas = np.zeros((360, 640, 3), dtype=np.uint8)
        source = self.processor.source
        connected = source.is_connected if source else False
        message = "waiting for frames..." if connected else "camera not connected"
        cv2.putText(canvas, message, (30, 180), _FONT, 0.8, (60, 180, 255), 2, cv2.LINE_AA)
        return canvas

    def _draw_overlay(self, canvas: np.ndarray) -> None:
        processor = self.processor
        pipeline = processor.pipeline
        timings = pipeline.timings.as_dict()

        lines = [
            f"view: {self.view}   in {processor.input_fps.fps:5.1f} fps"
            f"   out {processor.output_fps.fps:5.1f} fps",
            f"latency {processor.status()['latency_ms']:6.1f} ms"
            f"   pipeline {pipeline.timings.total_ms:5.2f} ms",
        ]

        boundary = pipeline.get("boundary")
        if boundary is not None:
            status = boundary.status()
            lines.append(
                f"tv: {status.get('origin')} conf {status.get('confidence', 0):.2f}"
                f"  {'locked' if status.get('locked') else 'searching'}"
            )

        bars = pipeline.get("blackbars")
        if bars is not None:
            pixels = bars.status().get("pixels", {})
            aspect = bars.status().get("content_aspect")
            lines.append(
                f"bars t{pixels.get('top', 0)} b{pixels.get('bottom', 0)} "
                f"l{pixels.get('left', 0)} r{pixels.get('right', 0)}"
                + (f"   {aspect:.2f}:1" if aspect else "")
            )

        slowest = sorted(timings.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if slowest:
            lines.append("  ".join(f"{n} {v:.1f}ms" for n, v in slowest))

        disabled = [s.name for s in pipeline.stages if not s.enabled]
        if disabled:
            lines.append("disabled: " + ", ".join(disabled))

        self._draw_panel(canvas, lines, origin=(8, 8), colour=(80, 255, 120))

    def _draw_help(self, canvas: np.ndarray) -> None:
        lines = [f"{key:<9} {text}" for key, text in KEY_HELP]
        height = canvas.shape[0]
        self._draw_panel(
            canvas,
            ["keyboard"] + lines,
            origin=(8, max(30, height - 20 * (len(lines) + 2))),
            colour=(220, 220, 220),
        )

    @staticmethod
    def _draw_panel(canvas, lines, origin, colour) -> None:
        x, y = origin
        width = max((len(line) for line in lines), default=0) * 8 + 16
        height = len(lines) * 18 + 10
        overlay = canvas.copy()
        cv2.rectangle(overlay, (x, y), (x + width, y + height), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, canvas, 0.45, 0, canvas)
        for i, line in enumerate(lines):
            cv2.putText(
                canvas, line, (x + 8, y + 18 + i * 18), _FONT, 0.42, colour, 1, cv2.LINE_AA
            )

    # -- input -------------------------------------------------------------

    def handle_key(self, key: int) -> bool:
        """Returns False to quit."""
        char = chr(key) if 32 <= key < 127 else ""

        if key in (27, ord("q")):
            return False
        if char == " ":
            self.paused = not self.paused
        elif char == "g":
            self.view = "grid" if self.view != "grid" else "output"
        elif char == "o":
            self.show_overlay = not self.show_overlay
        elif char == "h":
            self.show_help = not self.show_help
        elif char == "r":
            self.processor.force_recalibration()
            log.info("Recalibration requested from the debug window")
        elif char == "s":
            self._save_snapshot()
        elif char == "w":
            self.processor.save()
        elif char in ("[", "]"):
            self._cycle_view(-1 if char == "[" else 1)
        elif char.isdigit():
            self._select_view(int(char))
        elif char in STAGE_KEYS:
            name = STAGE_KEYS[char]
            result = self.processor.call(lambda: self.processor.pipeline.toggle(name))
            if result is None:
                log.info("Stage %s is not in the pipeline", name)
            else:
                log.info("Stage %s %s", name, "enabled" if result else "disabled")
        return True

    def _view_names(self) -> list[str]:
        return ["grid"] + self.processor.available_views()

    def _select_view(self, index: int) -> None:
        names = self._view_names()
        if 0 <= index < len(names):
            self.view = names[index]

    def _cycle_view(self, delta: int) -> None:
        names = self._view_names()
        try:
            index = names.index(self.view)
        except ValueError:
            index = 0
        self.view = names[(index + delta) % len(names)]

    def _save_snapshot(self) -> None:
        canvas = self.render()
        if canvas is None:
            return
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self._snapshot_dir / f"{stamp}-{self.view}.png"
        cv2.imwrite(str(path), canvas)
        log.info("Snapshot saved to %s", path)
