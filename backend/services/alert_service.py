from datetime import datetime
from pathlib import Path
import json

ALERTS_FILE = Path("alerts.json")


def save_alert(vehicle):
    """Save overload alert to alerts.json"""

    alert = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S"),
        "vehicle_type": vehicle["vehicle_type"],
        "registration_number": vehicle["registration_number"],
        "confidence": vehicle["confidence"],
        "severity_score": vehicle["severity_score"],
        "status": vehicle["overload_status"],
        "location": vehicle["location"],
        "snapshot": vehicle["snapshot"],
        "timestamp": vehicle["timestamp"],
        "message": f"🚨 OVERLOADED {vehicle['vehicle_type']} detected at {vehicle['location']}"
    }

    alerts = []

    if ALERTS_FILE.exists():
        with open(ALERTS_FILE, "r") as f:
            alerts = json.load(f)

    alerts.insert(0, alert)

    with open(ALERTS_FILE, "w") as f:
        json.dump(alerts, f, indent=4)

    return alert


def get_alerts():
    if ALERTS_FILE.exists():
        with open(ALERTS_FILE, "r") as f:
            return json.load(f)

    return []