"""
OverWatch AI live camera service.

- Opens the camera lazily instead of during module import.
- Uses DirectShow on Windows, with a generic OpenCV fallback.
- Keeps one persistent tracking/OCR state for the live stream.
- Prevents multiple browser clients from running inference concurrently.
- Uses a small camera buffer to reduce display latency.
"""

import atexit
import logging
import os
import threading
import time
from typing import Optional

import cv2

from services.yolo_service import detect_frame

logger = logging.getLogger("overwatch.camera")

# Camera settings. Use 1280x720 as a practical starting point for CPU inference.
# Increase to 1920x1080 only after confirming acceptable live FPS.
CAMERA_INDEX = int(os.getenv("OVERWATCH_CAMERA_INDEX", "0"))
FRAME_WIDTH = int(os.getenv("OVERWATCH_CAMERA_WIDTH", "1280"))
FRAME_HEIGHT = int(os.getenv("OVERWATCH_CAMERA_HEIGHT", "720"))
CAMERA_FPS = int(os.getenv("OVERWATCH_CAMERA_FPS", "15"))
JPEG_QUALITY = int(os.getenv("OVERWATCH_JPEG_QUALITY", "88"))
DEFAULT_LOCATION = os.getenv(
    "OVERWATCH_CAMERA_LOCATION", "Polokwane Weighbridge"
)

# Camera access and live inference are separately guarded.
_camera_lock = threading.RLock()
_stream_lock = threading.Lock()

_camera: Optional[cv2.VideoCapture] = None
_camera_backend = "not opened"
_camera_last_open_attempt = 0.0
_REOPEN_INTERVAL_SECONDS = 2.0


def _open_camera() -> Optional[cv2.VideoCapture]:
    """Open camera with a Windows-specific backend first, then fallback."""
    global _camera, _camera_backend, _camera_last_open_attempt

    now = time.monotonic()
    if now - _camera_last_open_attempt < _REOPEN_INTERVAL_SECONDS:
        return None
    _camera_last_open_attempt = now

    backends = []
    if os.name == "nt":
        backends.append(("DirectShow", cv2.CAP_DSHOW))
    backends.append(("OpenCV default", cv2.CAP_ANY))

    for backend_name, backend in backends:
        cap = cv2.VideoCapture(CAMERA_INDEX, backend)
        if not cap.isOpened():
            cap.release()
            continue

        # MJPG often reduces USB bandwidth for UVC webcams. If unsupported,
        # OpenCV simply continues with the camera's default format.
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)

        # A one-frame buffer helps avoid displaying old queued frames.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        # Verify an actual frame is available before publishing the handle.
        ok, test_frame = cap.read()
        if not ok or test_frame is None or test_frame.size == 0:
            cap.release()
            continue

        _camera = cap
        _camera_backend = backend_name
        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)

        logger.info(
            "Camera opened: index=%s backend=%s resolution=%sx%s reported_fps=%.2f",
            CAMERA_INDEX,
            _camera_backend,
            actual_width,
            actual_height,
            actual_fps,
        )
        return _camera

    logger.error(
        "Unable to open camera index %s. Check camera connection, permissions, "
        "and whether another application is using it.",
        CAMERA_INDEX,
    )
    return None


def _get_camera() -> Optional[cv2.VideoCapture]:
    global _camera
    with _camera_lock:
        if _camera is not None and _camera.isOpened():
            return _camera
        if _camera is not None:
            try:
                _camera.release()
            except Exception:
                pass
        _camera = None
        return _open_camera()


def _read_frame():
    """Read the newest available frame; reconnect if the camera disconnects."""
    global _camera

    with _camera_lock:
        cap = _get_camera()
        if cap is None:
            return None

        ok, frame = cap.read()
        if ok and frame is not None and frame.size:
            return frame

        logger.warning("Camera frame read failed; reconnecting camera.")
        try:
            cap.release()
        except Exception:
            pass
        _camera = None
        return None


def generate_frames(location=DEFAULT_LOCATION):
    """
    Yield annotated MJPEG frames for FastAPI's /video_feed endpoint.

    The tracking state is persistent for the lifetime of this stream. Only one
    generator may run inference at a time, avoiding competing tracker states
    when the dashboard is opened in multiple tabs.
    """
    acquired = _stream_lock.acquire(blocking=False)
    if not acquired:
        logger.warning(
            "A live camera stream is already active; refusing a second inference stream."
        )
        return

    saved_tracks = set()
    plate_cache = {}
    records_by_id = {}
    frame_index = 0
    last_report = time.monotonic()
    processed_frames = 0
    effective_fps = 0.0

    try:
        while True:
            frame = _read_frame()
            if frame is None:
                time.sleep(0.25)
                continue

            frame_index += 1
            started = time.perf_counter()

            try:
                result = detect_frame(
                    frame=frame,
                    location=location or DEFAULT_LOCATION,
                    saved_tracks=saved_tracks,
                    plate_cache=plate_cache,
                    frame_index=frame_index,
                    records_by_id=records_by_id,
                )

                if not isinstance(result, tuple) or len(result) < 2:
                    raise RuntimeError(
                        "detect_frame must return (annotated_frame, vehicles)."
                    )

                annotated, vehicles = result[0], result[1]
                if annotated is None or not hasattr(annotated, "shape"):
                    raise RuntimeError("YOLO returned an invalid annotated frame.")

                # Ensure a valid BGR image before JPEG encoding.
                if annotated.ndim == 2:
                    annotated = cv2.cvtColor(
                        annotated, cv2.COLOR_GRAY2BGR
                    )
                elif annotated.ndim == 3 and annotated.shape[2] == 4:
                    annotated = cv2.cvtColor(
                        annotated, cv2.COLOR_BGRA2BGR
                    )

                encoded, buffer = cv2.imencode(
                    ".jpg",
                    annotated,
                    [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
                )
                if not encoded:
                    logger.warning("Could not JPEG-encode live camera frame.")
                    continue

                processed_frames += 1
                elapsed = max(time.perf_counter() - started, 1e-6)
                instant_fps = 1.0 / elapsed
                effective_fps = (
                    instant_fps if effective_fps == 0
                    else 0.85 * effective_fps + 0.15 * instant_fps
                )

                now = time.monotonic()
                if now - last_report >= 10:
                    logger.info(
                        "LIVE CAMERA: frame=%d vehicles=%d processing_fps=%.2f "
                        "total_frames=%d",
                        frame_index,
                        len(vehicles) if isinstance(vehicles, (list, tuple)) else 0,
                        effective_fps,
                        processed_frames,
                    )
                    last_report = now

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-cache, no-store\r\n"
                    b"\r\n"
                    + buffer.tobytes()
                    + b"\r\n"
                )

            except GeneratorExit:
                raise
            except Exception:
                logger.exception(
                    "Camera frame processing failed at frame %d", frame_index
                )
                # Brief pause avoids a tight exception loop.
                time.sleep(0.05)

    finally:
        _stream_lock.release()
        logger.info("Live camera stream ended.")


def release_camera():
    """Release the physical camera safely during application shutdown."""
    global _camera
    with _camera_lock:
        if _camera is not None:
            try:
                _camera.release()
                logger.info("Camera released.")
            except Exception:
                logger.exception("Error while releasing camera.")
            finally:
                _camera = None


def get_camera_status():
    """Return basic camera state for diagnostics/health checks."""
    with _camera_lock:
        opened = _camera is not None and _camera.isOpened()
        return {
            "opened": opened,
            "camera_index": CAMERA_INDEX,
            "backend": _camera_backend,
            "requested_width": FRAME_WIDTH,
            "requested_height": FRAME_HEIGHT,
            "requested_fps": CAMERA_FPS,
        }


atexit.register(release_camera)
