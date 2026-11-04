
"use client";

const STREAM_URL = "http://127.0.0.1:8000/video_feed";

export default function CameraStream() {
  return (
    <div className="w-full overflow-hidden rounded-xl bg-black">
      <img
        src={STREAM_URL}
        alt="OverWatch AI live vehicle detection feed"
        className="block h-auto max-h-[75vh] w-full object-contain"
      />
    </div>
  );
}
