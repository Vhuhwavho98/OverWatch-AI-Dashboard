import supervision as sv

tracker = sv.ByteTrack(
    track_activation_threshold=0.25,
    lost_track_buffer=30,
    minimum_matching_threshold=0.8,
    frame_rate=30,
)

saved_tracker_ids = set()
active_vehicles = {}

def update_tracker(detections):
    if detections is None or len(detections) == 0:
        return detections

    tracked = tracker.update_with_detections(detections)

    if tracked.tracker_id is None:
        return tracked

    return tracked

def register_vehicle(tracker_id, vehicle_type, confidence):
    active_vehicles[tracker_id] = {
        "vehicle_type": vehicle_type,
        "confidence": confidence,
    }

def is_saved(tracker_id):
    return tracker_id in saved_tracker_ids

def mark_saved(tracker_id):
    saved_tracker_ids.add(tracker_id)

def cleanup_tracker(current_ids):
    for tracker_id in list(active_vehicles.keys()):
        if tracker_id not in current_ids:
            active_vehicles.pop(tracker_id, None)
            saved_tracker_ids.discard(tracker_id)

def reset_tracker():
    active_vehicles.clear()
    saved_tracker_ids.clear()