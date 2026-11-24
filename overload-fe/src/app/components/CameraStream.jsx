
import { useEffect, useRef, useState } from "react";

export default function CameraStream() {
  const videoRef = useRef(null);
  const streamRef = useRef(null);

  const [status, setStatus] = useState("connecting");
  const [error, setError] = useState("");

  useEffect(() => {
    let mounted = true;

    async function startCamera() {
      try {
        if (!navigator.mediaDevices?.getUserMedia) {
          throw new Error("Camera access requires HTTPS or localhost.");
        }

        const stream = await navigator.mediaDevices.getUserMedia({
          video: {
            facingMode: "environment",
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
          audio: false,
        });

        if (!mounted) {
          stream.getTracks().forEach(track => track.stop());
          return;
        }

        streamRef.current = stream;
        videoRef.current.srcObject = stream;
        await videoRef.current.play();

        if (mounted) setStatus("connected");
      } catch (err) {
        if (!mounted) return;

        setStatus("error");
        setError(
          err.name === "NotAllowedError"
            ? "Please allow camera access in your browser."
            : err.message || "Unable to open camera."
        );
      }
    }

    startCamera();

    return () => {
      mounted = false;
      streamRef.current?.getTracks().forEach(track => track.stop());
      streamRef.current = null;
    };
  }, []);

  return (
    <div className="w-full overflow-hidden rounded-xl bg-black">
      <video
        ref={videoRef}
        autoPlay
        muted
        playsInline
        className="block h-auto max-h-[75vh] w-full object-contain"
      />

      {status === "connecting" && (
        <p className="p-3 text-center text-white">
          Opening camera...
        </p>
      )}

      {status === "error" && (
        <p className="p-3 text-center text-red-400">
          {error}
        </p>
      )}

      {status === "connected" && (
        <p className="p-2 text-center text-green-400">
          ● Camera connected
        </p>
      )}
    </div>
  );
}
