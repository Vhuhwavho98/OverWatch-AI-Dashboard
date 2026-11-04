from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

MODEL_PATH = BASE_DIR / "yolo11n.pt"

UPLOAD_DIR = BASE_DIR / "uploads"
IMAGE_DIR = UPLOAD_DIR / "images"
VIDEO_DIR = UPLOAD_DIR / "videos"
SNAPSHOT_DIR = UPLOAD_DIR / "snapshots"

for folder in [UPLOAD_DIR, IMAGE_DIR, VIDEO_DIR, SNAPSHOT_DIR]:
    folder.mkdir(parents=True, exist_ok=True)