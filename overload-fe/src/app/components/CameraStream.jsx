
"use client";


const API = "https://name-contribution-rapids-programmer.trycloudflare.com";

export const API_BASE_URL = API;
export const cameraFeed = `${API}/video_feed`;

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
