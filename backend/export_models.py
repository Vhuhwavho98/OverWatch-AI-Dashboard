
from pathlib import Path
from ultralytics import YOLO

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "models"

models = [
    ("yolo11n.pt", 512),
    ("yolo26n.pt", 416),
]

for filename, image_size in models:
    model_path = MODEL_DIR / filename

    if not model_path.exists():
        print(f"Missing model: {model_path}")
        continue

    print(f"\nExporting {filename} to NCNN...")

    model = YOLO(str(model_path))

    exported = model.export(
        format="ncnn",
        imgsz=image_size,
        half=False
    )

    print(f"Successfully exported: {exported}")

print("\nModel export process completed.")
