"""How many people are in the room.

PIR already answers "is anyone there?". It cannot answer "how many", and it
cannot tell a still person from an empty room. This does both.

Four backends behind one interface, same shape as `sense/reader`:

    app-lab  Arduino detection Brick    production board path
    mock     scripted counts            tests, no camera
    opencv   MobileNet-SSD via cv2.dnn  desktop development fallback
    fomo     Edge Impulse FOMO          optional room-trained evaluation path

Nothing above this file knows which one is running.

PRIVACY — a design rule, not a nicety. The contest bans harmful surveillance.
Frames are counted and discarded. Runtime code stores no image. Explicit
dataset collection is a separate human-invoked command. Telemetry logs count,
confidence, source, health, and relative luminance.
"""
from __future__ import annotations

import json
import statistics
import time
import threading
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

MODELS = Path(__file__).resolve().parent / "models"

# MobileNet-SSD's label set; we only ever look at one of them.
PERSON_CLASS_ID = 15


@dataclass
class CountFrame:
    """One occupancy observation. `valid` is the field that matters — the
    controller must never act on a count it does not trust."""
    count: Optional[int] = None
    confidence: float = 0.0          # evidence strength for the reported count:
                                     # the median per-frame best score across the
                                     # frames that saw anyone. Not a per-box score —
                                     # the count is a vote, so its confidence must be
                                     # too, and a median resists one lucky frame.
    valid: bool = False
    fault: str = ""
    inference_ms: float = 0.0
    luminance: Optional[float] = None  # relative 0..1 mean frame light, never lux
    at: float = field(default_factory=time.time)
    source: str = ""

    @property
    def occupied(self) -> Optional[bool]:
        return None if self.count is None else self.count > 0

    def age_s(self) -> float:
        return time.time() - self.at


class PersonCounter(ABC):
    name = "unnamed"
    # True when the backend infers continuously on its own, so reading a count
    # costs nothing and the sampling policy has no work to save. False for
    # backends where each count() triggers an inference.
    CONTINUOUS = False

    def __init__(self, **kwargs):
        self.config = kwargs
        self._last_good: Optional[CountFrame] = None

    @abstractmethod
    def _count_raw(self) -> CountFrame:
        """Produce one unvalidated count. Subclasses implement only this."""

    def open(self) -> "PersonCounter":
        return self

    def close(self) -> None:
        pass

    def count(self) -> CountFrame:
        """Never raises. A failed count is data, not an exception — the control
        loop has to keep running on a camera that got unplugged."""
        t0 = time.perf_counter()
        try:
            frame = self._count_raw()
        except Exception as exc:
            frame = CountFrame(fault=f"{type(exc).__name__}: {exc}")
        frame.inference_ms = (time.perf_counter() - t0) * 1000
        frame.source = self.name
        if frame.count is not None and frame.count >= 0:
            frame.valid, frame.fault = True, ""
            self._last_good = frame
        else:
            frame.valid = False
            frame.fault = frame.fault or "no count produced"
        return frame

    def last_good(self) -> Optional[CountFrame]:
        return self._last_good

    def health(self) -> dict:
        f = self._last_good
        return {"counter": self.name,
                "last_count": f.count if f else None,
                "age_s": round(f.age_s(), 1) if f else None,
                "inference_ms": round(f.inference_ms, 1) if f else None}


# ── registry ────────────────────────────────────────────────────────────
_COUNTERS: dict[str, Callable[..., PersonCounter]] = {}


def register_counter(name: str):
    def deco(cls):
        cls.name = name
        _COUNTERS[name] = cls
        return cls
    return deco


def build_counter(name: str, **kwargs) -> PersonCounter:
    if name not in _COUNTERS:
        raise KeyError(f"unknown counter {name!r}; have {sorted(_COUNTERS)}")
    return _COUNTERS[name](**kwargs)


def available_counters() -> list[str]:
    return sorted(_COUNTERS)


# ── Arduino App Lab Brick — production default on UNO Q ───────────────
@register_counter("app-lab")
class AppLabCounter(PersonCounter):
    """UNO Q's supported USB-camera object-detection Brick.

    The Brick owns capture and accelerated inference.  Its callback pushes the
    newest person detections here; the BreezeIQ policy pulls only when PIR or
    baseline timing asks for a count.  Frames are never stored or transmitted.
    """

    # Tuned against 250 measured frames of THIS room, person seated:
    #   per-frame scores  min 0.35 · p10 0.47 · median 0.66 · p90 0.79 · max 0.86
    #   person found in 42 % of frames at 0.3, 35.6 % at 0.5
    # 0.40 sits just under p10, so it keeps the weak-but-real detections that
    # 0.50 was discarding. The window vote below is what makes that safe: a
    # lower bar per frame costs nothing once agreement across frames is required.
    WINDOW_S = 10.0
    PRESENCE_FRACTION = 0.15
    CONTINUOUS = True          # the Brick infers whether or not we look

    def __init__(self, camera: int | str = 0, confidence: float = 0.40, **kwargs):
        super().__init__(**kwargs)
        self.camera_id = camera
        self.confidence = confidence
        self._lock = threading.Lock()
        self._latest: Optional[CountFrame] = None
        self._detector = None
        self._last_luma_at = 0.0
        self._luminance: Optional[float] = None
        self._window: deque = deque(maxlen=600)   # ~45 s at 13 fps

    def open(self) -> "AppLabCounter":
        try:
            from arduino.app_bricks.video_objectdetection import VideoObjectDetection
            from arduino.app_peripherals.camera import Camera
        except ImportError as exc:
            raise RuntimeError("Arduino App Lab video_object_detection Brick unavailable") from exc

        source = self.camera_id
        if isinstance(source, int):
            source = f"usb:{source}"
        camera = Camera(source, resolution=(640, 480), fps=5)
        self._detector = VideoObjectDetection(
            camera=camera, confidence=self.confidence, debounce_sec=0.0,
            camera_preview=True)
        # start() opens the camera and sets the Brick's `_is_running` flag, which
        # is what its loops gate on.
        self._detector.start()

        # camera_loop captures frames and pushes them to the runner's tcp:5050.
        # It is decorated `@brick.execute`, so App Lab launches it once at app
        # startup — but this counter is constructed later, from the control
        # thread, so by then it has either not been scheduled or has already
        # fallen out of `while self._is_running.is_set()` against a flag that was
        # still clear. Driving it here is the whole lifecycle; it retries
        # internally until stop() clears the flag.
        threading.Thread(target=self._detector.camera_loop,
                         name="applab-camera_loop", daemon=True).start()

        # Results are read HERE rather than through the Brick's on_detect_all,
        # because that callback is gated on `if len(detections) > 0` — an empty
        # room produces no callback at all, so a count of zero could never be
        # reported and "nobody here" was indistinguishable from "camera dead".
        # Zero is the count that releases the AC, so it has to be real.
        #
        # The runner emits a `classification` message per frame regardless of
        # what it finds (~13/s), over the websocket it documents on startup, and
        # it accepts more than one client. Reading it directly gives both the
        # true count and an honest liveness signal, with no reliance on the
        # gated callback.
        self._uri = getattr(self._detector, "_uri", "")
        if not self._uri:
            raise RuntimeError("Brick exposed no runner websocket URI")
        threading.Thread(target=self._read_results,
                         name="applab-results", daemon=True).start()
        return self

    def _read_results(self) -> None:
        """Consume `classification` frames from the model runner, forever.

        Every failure is announced once per distinct cause. A silent retry loop
        here is indistinguishable from a camera that sees an empty room, and
        that ambiguity costs hours to unpick.
        """
        try:
            from websockets.sync.client import connect
        except ImportError as exc:
            print(f"  [vision] websockets unavailable, no counts: {exc}", flush=True)
            return
        said = ""
        while self._detector is not None:
            try:
                with connect(self._uri, open_timeout=10) as ws:
                    print(f"  [vision] reading detections from {self._uri}", flush=True)
                    said = ""
                    while self._detector is not None:
                        message = ws.recv(timeout=15)
                        if isinstance(message, (bytes, bytearray, memoryview)):
                            message = bytes(message).decode("utf-8", "replace")
                        try:
                            payload = json.loads(message)
                        except ValueError:
                            continue
                        if payload.get("type") == "classification":
                            self._on_classification(payload)
            except Exception as exc:
                # A dropped runner must not kill the counter; the room still has
                # PIR. Back off and reconnect, but say why at least once.
                why = f"{type(exc).__name__}: {exc}"
                if why != said:
                    print(f"  [vision] detection stream lost — {why}", flush=True)
                    said = why
                time.sleep(2.0)

    def _on_classification(self, payload: dict) -> None:
        """One inference result -> one sample in the voting window."""
        boxes = (payload.get("result") or {}).get("bounding_boxes") or []
        scores = [float(b.get("value", 0.0)) for b in boxes
                  if b.get("label") == "person"
                  and float(b.get("value", 0.0)) >= self.confidence]
        now = time.time()
        with self._lock:
            self._window.append((now, len(scores), max(scores, default=0.0)))
            cutoff = now - self.WINDOW_S
            while self._window and self._window[0][0] < cutoff:
                self._window.popleft()
            self._latest = self._vote(now)

    def _vote(self, now: float) -> CountFrame:
        """Turn the window of per-frame samples into one trustworthy count.

        Measured on this room: a stationary person is found in only ~42 % of
        frames, with scores from 0.35 to 0.86. So a single frame answers the
        wrong question — "was the model confident THIS instant", not "is someone
        in the room". Last-frame-wins reported an empty room every time the
        detector blinked.

        Presence therefore needs only a MINORITY of frames to agree
        (PRESENCE_FRACTION), because a person who turns away or is briefly
        occluded genuinely disappears, while a false positive is rare and
        uncorrelated between frames. Once present, the count is the MEDIAN of
        the frames that saw anyone — median, not max, so one spurious second
        body cannot inflate it.
        """
        window = list(self._window)
        seen = [count for _, count, _ in window if count > 0]
        strengths = [s for _, count, s in window if count > 0]
        present = window and (len(seen) / len(window)) >= self.PRESENCE_FRACTION
        count = int(statistics.median(sorted(seen))) if present else 0
        strength = statistics.median(sorted(strengths)) if present else 0.0
        return CountFrame(count=count, confidence=strength,
                          valid=True, luminance=self._luminance,
                          at=now, source=self.name)

    def close(self) -> None:
        if self._detector is not None:
            try:
                self._detector.stop()          # releases the camera device
            finally:
                self._detector = None

    @staticmethod
    def _boxes(detections: dict) -> list:
        people = detections.get("person", []) if isinstance(detections, dict) else []
        if isinstance(people, list):
            return people
        if people is None:
            return []
        return [people]

    def _on_detections(self, detections: dict, frame: Optional[bytes] = None) -> None:
        boxes = self._boxes(detections)
        scores = []
        for box in boxes:
            if isinstance(box, dict):
                score = box.get("confidence", box.get("value"))
            else:
                score = box if isinstance(box, (int, float)) else None
            if score is not None:
                scores.append(float(score))

        # Ambient light from the preview frame is NOT available. The Brick's only
        # callback is `on_detect_all(Callable[[dict], None])` — a labels dict and
        # nothing else; its public API (camera_loop, object_detection_loop,
        # on_detect, on_detect_all, override_threshold, start, stop) exposes no
        # frame anywhere. `frame` therefore stays None and `luminance` stays
        # None, which `fuse_light` already reads as "no camera evidence" and
        # falls back to the LDR. Kept as a parameter so a future Brick that does
        # hand over a frame needs no change here.
        now = time.time()

        observed = CountFrame(
            count=len(boxes),
            confidence=min(scores, default=0.0),
            valid=True,
            luminance=self._luminance,
            at=now,
            source=self.name,
        )
        with self._lock:
            self._latest = observed

    DETECTION_TTL_S = 15.0

    def _count_raw(self) -> CountFrame:
        """The newest inference result, or a fault.

        Every frame the runner processes updates `_latest`, empty rooms included,
        so a stale `_latest` means the pipeline itself stopped — never that the
        room emptied. Reporting a fault there rather than zero is the safe
        direction: a fabricated zero switches the AC off on an occupant.
        """
        with self._lock:
            latest = self._latest
        if latest is None:
            return CountFrame(fault="App Lab camera detector is starting")
        if latest.age_s() > self.DETECTION_TTL_S:
            return CountFrame(
                fault=f"App Lab camera detections stale for {latest.age_s():.0f}s")
        return CountFrame(count=latest.count, confidence=latest.confidence,
                          luminance=latest.luminance, at=latest.at)


# ── mock ────────────────────────────────────────────────────────────────
@register_counter("mock")
class MockCounter(PersonCounter):
    """Scripted counts. Lets the sampling policy be tested and tuned with no
    camera, no model and no room — the same trick `MockRoom` plays for physics."""

    def __init__(self, script: Optional[list[int]] = None, **kwargs):
        super().__init__(**kwargs)
        self.script = list(script) if script else [0]
        self.i = 0

    def _count_raw(self) -> CountFrame:
        n = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return CountFrame(count=n, confidence=1.0)


# ── opencv / MobileNet-SSD ──────────────────────────────────────────────
@register_counter("opencv")
class OpenCVCounter(PersonCounter):
    """MobileNet-SSD person detection through OpenCV's DNN module.

    Chosen over YOLO deliberately: no torch, ~23 MB of weights, and CPU
    inference that ports to the board's ARM Debian side unchanged. Accuracy is
    adequate for counting people in one room — this is not a crowd counter.

    `camera=0` is the Mac's built-in FaceTime camera. macOS will prompt for
    camera permission the first time; grant it to the terminal app.
    """

    CONFIDENCE = 0.45          # below this a box is noise, not a person
    NMS_OVERLAP = 0.30         # two boxes on one torso is one person

    def __init__(self, camera: int | str = 0, confidence: float = CONFIDENCE,
                 warmup_frames: int = 5, **kwargs):
        super().__init__(**kwargs)
        self.camera_id = camera
        self.confidence = confidence
        self.warmup_frames = warmup_frames
        self._cap = None
        self._net = None
        self._still = None
        self.last_boxes: list[tuple[int, int, int, int, float]] = []

    def open(self) -> "OpenCVCounter":
        import cv2
        proto = MODELS / "MobileNetSSD_deploy.prototxt"
        weights = MODELS / "MobileNetSSD_deploy.caffemodel"
        if not weights.exists():
            raise FileNotFoundError(
                f"{weights} missing — run tools/fetch-vision-model.sh")
        self._net = cv2.dnn.readNetFromCaffe(str(proto), str(weights))

        # A path means a still image: same pipeline, no camera. Useful for
        # tuning against saved room photos, and the only way to test where
        # camera permission is unavailable.
        if isinstance(self.camera_id, str) and Path(self.camera_id).exists():
            self._still = cv2.imread(self.camera_id)
            if self._still is None:
                raise RuntimeError(f"could not decode image {self.camera_id!r}")
            return self

        self._cap = cv2.VideoCapture(self.camera_id)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"camera {self.camera_id!r} would not open.\n"
                "  macOS: System Settings > Privacy & Security > Camera, then\n"
                "  enable it for Terminal/iTerm and run this again from that app.\n"
                "  Or point at a still: --camera path/to/room.jpg")
        # A webcam's first frames are auto-exposure garbage. Counting them
        # produces a phantom person or misses a real one.
        for _ in range(self.warmup_frames):
            self._cap.read()
        return self

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _count_raw(self) -> CountFrame:
        import cv2
        import numpy as np
        if self._net is None:
            raise RuntimeError("counter not opened — call open() first")

        if self._still is not None:
            frame = self._still.copy()
        else:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                raise RuntimeError("camera read failed")

        h, w = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            cv2.resize(frame, (300, 300)), 0.007843, (300, 300), 127.5)
        self._net.setInput(blob)
        detections = self._net.forward()

        boxes, scores = [], []
        for i in range(detections.shape[2]):
            score = float(detections[0, 0, i, 2])
            class_id = int(detections[0, 0, i, 1])
            if class_id != PERSON_CLASS_ID or score < self.confidence:
                continue
            x1, y1, x2, y2 = (detections[0, 0, i, 3:7] *
                              np.array([w, h, w, h])).astype(int)
            boxes.append([int(x1), int(y1), int(x2 - x1), int(y2 - y1)])
            scores.append(score)

        # Without NMS one person standing near the lens can register as three.
        keep = cv2.dnn.NMSBoxes(boxes, scores, self.confidence,
                                self.NMS_OVERLAP) if boxes else []
        idx = [int(i) for i in np.array(keep).flatten()] if len(keep) else []

        self.last_boxes = [(*boxes[i], scores[i]) for i in idx]
        self._last_bgr = frame                              # for debug snapshots only
        luminance = float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean() / 255.0)
        return CountFrame(count=len(idx),
                          confidence=min([scores[i] for i in idx], default=0.0),
                          luminance=luminance)

    def save_debug_frame(self, path: str | Path) -> Optional[Path]:
        """Write an annotated frame. Only ever called by a human tuning the rig —
        the control loop never stores an image."""
        import cv2
        if getattr(self, "_last_bgr", None) is None:
            return None
        img = self._last_bgr.copy()
        for (x, y, bw, bh, score) in self.last_boxes:
            cv2.rectangle(img, (x, y), (x + bw, y + bh), (0, 220, 0), 2)
            cv2.putText(img, f"{score:.2f}", (x, max(14, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1)
        cv2.putText(img, f"count={len(self.last_boxes)}", (10, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 220, 255), 2)
        path = Path(path)
        cv2.imwrite(str(path), img)
        return path


# ── Edge Impulse FOMO — the production path ─────────────────────────────
# The live camera preview is a same-process handoff: the counter runs in the
# control-loop thread and the console serves HTTP in another thread of the SAME
# App Lab process (app/breezeiq/main.py starts both). A running camera counter
# registers itself here; the console reads the latest annotated JPEG from it.
# None when no camera counter is live (dry runs, tests, the PIR-only fallback).
_LIVE_PREVIEW = {"counter": None}


def live_preview_jpeg():
    """Latest annotated camera frame as JPEG bytes, or None if no live counter.

    Streaming a live view is not storing one: these bytes are made on demand from
    the frame in hand and never written to disk, which is the same contract the
    App Lab Brick's own preview honoured.
    """
    counter = _LIVE_PREVIEW.get("counter")
    return counter.preview_jpeg() if counter is not None else None


# ── YOLO11 via ONNX Runtime — the accurate path ─────────────────────────
def _nms(boxes, scores, iou_max):
    """Greedy non-maximum suppression in numpy.

    cv2.dnn.NMSBoxes is unavailable: the board ships an Arduino OpenCV build
    with the whole dnn module compiled out ("Disabled: dnn world"), which is
    also why inference below is ONNX Runtime rather than cv2.dnn.
    """
    import numpy as np
    order = scores.argsort()[::-1]
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = boxes[:, 0] + boxes[:, 2], boxes[:, 1] + boxes[:, 3]
    areas = boxes[:, 2] * boxes[:, 3]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(x1[i], x1[rest])
        yy1 = np.maximum(y1[i], y1[rest])
        xx2 = np.minimum(x2[i], x2[rest])
        yy2 = np.minimum(y2[i], y2[rest])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        iou = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[iou <= iou_max]
    return keep


@register_counter("yolo")
class YoloCounter(PersonCounter):
    """YOLO11s through ONNX Runtime, opening the camera itself.

    WHY THIS REPLACED THE BRICK. Scored on 120 labelled frames of this room
    (3 people x107, 2 x7, 1 x6), counting exactly right:

        App Lab Brick (MobileNet-SSD)  0.10 exact   MAE 0.90
        MobileNet-SSD, threshold tuned 0.35         MAE 0.66
        YOLO11n                        0.59         MAE 0.42
        YOLO11s                        0.71         MAE 0.30   <- this
        YOLO11m                        0.61         MAE 0.39

    The Brick called three people "two" in 104 of 107 frames. It is not a
    tuning problem: a reclining or partly-occluded person is outside what
    MobileNet-SSD reliably sees, and the person on the bed scored 0.44 when
    it was seen at all. YOLO11m being WORSE than YOLO11s is measured, not a
    typo — bigger is not better here.

    It costs ~2.5 s per inference on four ARM cores, hence CONTINUOUS=False:
    `vision/policy.py` only asks for a count on a PIR edge or every five
    minutes, so the cost lands on events rather than on every tick.
    """

    CONTINUOUS = False
    INPUT = 640
    PERSON = 0                     # COCO class id, unlike MobileNet-SSD's 15
    NMS_IOU = 0.45
    THREADS = 3                    # leave one core for the comfort loop

    def __init__(self, camera: int | str = 0, confidence: float = 0.35,
                 model: str = "", **kwargs):
        super().__init__(**kwargs)
        self.camera_id = camera
        # 0.35 is the measured optimum, not a guess: 0.28/0.32/0.38/0.42 all
        # scored worse on the same frames.
        self.confidence = confidence
        self.model_path = model or str(MODELS / "yolo11s.onnx")
        self._session = None
        self._input_name = ""
        self._cap = None
        # Capture runs continuously so the preview is live; inference stays
        # on-demand (CONTINUOUS=False). Capture is milliseconds, inference is
        # ~2.5 s, so the two must not share a cadence.
        self._lock = threading.Lock()
        self._frame = None            # latest raw BGR, what inference reads
        self._boxes: list = []        # last kept boxes, drawn on the preview
        self._preview = None          # latest annotated JPEG bytes
        self._running = False
        self._grab_thread = None

    def open(self) -> "YoloCounter":
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime missing — it is declared in the app's "
                "python/requirements.txt and installed at app start") from exc
        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"{self.model_path} missing — see "
                                    "tools/fetch-vision-model.sh")
        options = ort.SessionOptions()
        options.intra_op_num_threads = self.THREADS
        self._session = ort.InferenceSession(
            self.model_path, options, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        import cv2
        self._cap = cv2.VideoCapture(self.camera_id)
        if not self._cap.isOpened():
            raise RuntimeError(f"camera {self.camera_id!r} would not open")
        self._running = True
        self._grab_thread = threading.Thread(
            target=self._grab_loop, name="yolo-capture", daemon=True)
        self._grab_thread.start()
        _LIVE_PREVIEW["counter"] = self
        return self

    def close(self) -> None:
        self._running = False
        if _LIVE_PREVIEW.get("counter") is self:
            _LIVE_PREVIEW["counter"] = None
        if self._grab_thread is not None:
            self._grab_thread.join(timeout=2.0)
            self._grab_thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._session = None

    PREVIEW_FPS = 8                    # live-view cadence; cheap, unlike inference
    PREVIEW_WIDTH = 640                # downscale for the wire, not for the model

    def _grab_loop(self) -> None:
        """Continuously buffer the newest frame and re-encode the preview.

        A dropped read must not kill the thread — the camera can re-enumerate
        under load — so back off and keep trying while the counter is open.
        """
        import time
        period = 1.0 / self.PREVIEW_FPS
        while self._running:
            ok, frame = (False, None)
            try:
                ok, frame = self._cap.read()
            except Exception:
                ok = False
            if not ok or frame is None:
                time.sleep(0.2)
                continue
            with self._lock:
                self._frame = frame
            self._encode_preview(frame)
            time.sleep(period)

    def _encode_preview(self, frame) -> None:
        import cv2
        with self._lock:
            boxes = list(self._boxes)
        height, width = frame.shape[:2]
        if width > self.PREVIEW_WIDTH:
            scale = self.PREVIEW_WIDTH / float(width)
            img = cv2.resize(frame, (self.PREVIEW_WIDTH, int(height * scale)))
            boxes = [[int(v * scale) for v in b] for b in boxes]
        else:
            img = frame.copy()
        for (x, y, bw, bh) in boxes:
            cv2.rectangle(img, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            with self._lock:
                self._preview = buf.tobytes()

    def preview_jpeg(self):
        with self._lock:
            return self._preview

    def _count_raw(self) -> CountFrame:
        import cv2
        import numpy as np
        if self._session is None:
            raise RuntimeError("counter not opened — call open() first")
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            raise RuntimeError("no camera frame yet")
        height, width = frame.shape[:2]

        # Square-pad rather than stretch: a stretched person is a shape the
        # model was never trained on, and the count is what suffers.
        side = max(height, width)
        padded = np.zeros((side, side, 3), np.uint8)
        padded[:height, :width] = frame
        blob = cv2.resize(padded, (self.INPUT, self.INPUT))
        blob = cv2.cvtColor(blob, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None]

        out = self._session.run(None, {self._input_name: blob})[0]
        out = out[0].T                       # (8400, 4 + 80 classes)
        scores = out[:, 4 + self.PERSON]
        hit = scores >= self.confidence
        if not hit.any():
            with self._lock:
                self._boxes = []
            return CountFrame(count=0, confidence=0.0,
                              luminance=self._luma(frame))
        scale = side / float(self.INPUT)
        xywh, kept = out[hit, :4], scores[hit]
        boxes = np.stack([(xywh[:, 0] - xywh[:, 2] / 2) * scale,
                          (xywh[:, 1] - xywh[:, 3] / 2) * scale,
                          xywh[:, 2] * scale,
                          xywh[:, 3] * scale], axis=1)
        keep = _nms(boxes, kept, self.NMS_IOU)
        with self._lock:
            self._boxes = [[int(v) for v in boxes[i]] for i in keep]
        return CountFrame(count=len(keep),
                          confidence=float(kept[keep].min()),
                          luminance=self._luma(frame))

    @staticmethod
    def _luma(frame) -> float:
        import cv2
        return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean() / 255.0)


@register_counter("fomo")
class FomoCounter(PersonCounter):
    """Edge Impulse FOMO via the EI Linux runner (.eim, AArch64 on the board).

    This is what closes promise #9: a model trained on ~200 frames of THIS
    room, running on the board's own CPU. FOMO returns centroids, not boxes —
    the count is simply how many it found. At 96×96 greyscale a frame is not
    recognisable video, which is the privacy argument made in silicon.

    Runner code is complete; only the .eim file is pending (train it per
    export it for "Linux (AARCH64)").

        pip install edge_impulse_linux     # on the board
        ./tools/run.sh vision once --counter fomo --model models/person.eim
    """

    def __init__(self, model: str = "", camera: int | str = 0, **kwargs):
        super().__init__(**kwargs)
        self.model_path = model or str(MODELS / "person.eim")
        self.camera_id = camera
        self._runner = None
        self._cap = None

    def open(self) -> "FomoCounter":
        try:
            from edge_impulse_linux.image import ImageImpulseRunner
        except ImportError as exc:
            raise RuntimeError(
                "edge_impulse_linux not installed — pip install edge_impulse_linux "
                "(on the board, not the Mac)") from exc
        if not Path(self.model_path).exists():
            raise FileNotFoundError(
                f"{self.model_path} missing — train and export the FOMO model, "
                "an Edge Impulse .eim exported for Linux (AARCH64)")
        self._runner = ImageImpulseRunner(self.model_path)
        self._runner.init()
        import cv2
        self._cap = cv2.VideoCapture(self.camera_id)
        if not self._cap.isOpened():
            raise RuntimeError(f"camera {self.camera_id!r} would not open")
        return self

    def close(self) -> None:
        if self._runner is not None:
            self._runner.stop()
            self._runner = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _count_raw(self) -> CountFrame:
        import cv2
        if self._runner is None:
            raise RuntimeError("counter not opened — call open() first")
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise RuntimeError("camera read failed")
        features, cropped = self._runner.get_features_from_image(
            cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        result = self._runner.classify(features)
        boxes = result.get("result", {}).get("bounding_boxes", [])
        people = [b for b in boxes if b.get("label") == "person"
                  and b.get("value", 0) >= 0.5]
        luminance = float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean() / 255.0)
        return CountFrame(count=len(people),
                          confidence=min((b["value"] for b in people), default=0.0),
                          luminance=luminance)
