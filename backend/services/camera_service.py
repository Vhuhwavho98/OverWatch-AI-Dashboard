
import cv2
import time
import threading

from services.yolo_service import detect_frame


# ============================================================
# CAMERA CONFIGURATION
# ============================================================

CAMERA_INDEX = 0
FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
CAMERA_FPS = 15
JPEG_QUALITY = 95

DEFAULT_LOCATION = "Polokwane Weighbridge"


# ============================================================
# CAMERA INITIALISATION
# ============================================================

camera = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)

if not camera.isOpened():
    camera.release()
    camera = cv2.VideoCapture(CAMERA_INDEX)

if camera.isOpened():
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    camera.set(cv2.CAP_PROP_FPS, CAMERA_FPS)

    actual_width = int(camera.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(camera.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(
        f"Camera opened: {actual_width}x{actual_height}"
    )
else:
    print("WARNING: Could not open the camera.")

camera_lock = threading.Lock()


# ============================================================
# LIVE CAMERA STREAM
# ============================================================

def generate_frames(location=DEFAULT_LOCATION):
    """
    Capture camera frames, detect vehicles, and yield
    annotated JPEG frames for a FastAPI MJPEG stream.

    The original camera frame is passed to YOLO. JPEG
    compression is applied only to the displayed result.
    """

    saved_tracks = set()
    plate_cache = {}
    records_by_id = {}
    frame_index = 0

    while True:
        if not camera.isOpened():
            print("Camera is not available.")
            time.sleep(1)
            continue

        try:
            with camera_lock:
                success, frame = camera.read()

            if not success or frame is None:
                print("Unable to read camera frame.")
                time.sleep(0.1)
                continue

            frame_index += 1

            # Keep the original captured frame for detection.
            annotated, vehicles = detect_frame(
                frame=frame,
                location=location,
                saved_tracks=saved_tracks,
                plate_cache=plate_cache,
                frame_index=frame_index,
                records_by_id=records_by_id,
            )

            # Compress only the outgoing display frame.
            encoded, buffer = cv2.imencode(
                ".jpg",
                annotated,
                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
            )

            if not encoded:
                continue

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n"
                b"\r\n"
                + buffer.tobytes()
                + b"\r\n"
            )

        except GeneratorExit:
            break

        except Exception as exc:
            print(f"CAMERA PROCESSING ERROR: {exc}")
            time.sleep(0.1)
            continue


# ============================================================
# RELEASE CAMERA
# ============================================================

def release_camera():
    """Release the camera safely during application shutdown."""

    global camera

    with camera_lock:
        if camera is not None and camera.isOpened():
            camera.release()
            print("Camera released.")
