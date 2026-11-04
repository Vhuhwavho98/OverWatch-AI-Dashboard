
const API = "http://127.0.0.1:8000";

export const cameraFeed = `${API}/video_feed`;

// =======================================
// HELPERS
// =======================================

export function mediaUrl(path) {
  if (!path) return null;

  if (path.startsWith("blob:")) return path;

  if (/^https?:\/\//i.test(path)) return path;

  return `${API}${path.startsWith("/") ? "" : "/"}${path}`;
}

async function readResponse(response) {
  const data = await response.json();

  if (!response.ok) {
    const message =
      typeof data.detail === "string"
        ? data.detail
        : data.error || `Request failed: ${response.status}`;

    throw new Error(message);
  }

  return data;
}

// =======================================
// DASHBOARD
// =======================================

export async function getDashboard() {
  const res = await fetch(`${API}/dashboard`);
  return readResponse(res);
}

export async function getLatestDetection() {
  const res = await fetch(`${API}/dashboard/latest`);
  return readResponse(res);
}

export async function getDetections() {
  const res = await fetch(`${API}/detections`);
  return readResponse(res);
}

export async function getAlerts() {
  const res = await fetch(`${API}/alerts`);
  return readResponse(res);
}


export async function updateLocation(location) {
  const response = await fetch(`${API}/location`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      latitude: location.latitude,
      longitude: location.longitude,
    }),
  });

  if (!response.ok) {
    throw new Error("Failed to resolve GPS location");
  }

  return response.json();
}

// =======================================
// IMAGE UPLOAD
// =======================================

export async function uploadImage(file, location) {
  const formData = new FormData();

  formData.append("file", file);
  formData.append("location", location || "Polokwane Weighbridge");

  const res = await fetch(`${API}/upload/image`, {
    method: "POST",
    body: formData,
  });

  const data = await readResponse(res);

  return {
    ...data,
    preview_image: mediaUrl(data.preview_image),
  };
}

// =======================================
// VIDEO UPLOAD
// =======================================

export async function uploadVideo(file, location) {
  const formData = new FormData();

  formData.append("file", file);
  formData.append("location", location || "Polokwane Weighbridge");

  const res = await fetch(`${API}/upload/video`, {
    method: "POST",
    body: formData,
  });

  const data = await readResponse(res);

  return {
    ...data,
    preview_video: mediaUrl(data.preview_video),
  };
}

// =======================================
// CHECK BACKGROUND JOB STATUS
// =======================================

export async function getJobStatus(jobId, signal) {
  const res = await fetch(
    `${API}/jobs/${encodeURIComponent(jobId)}`,
    { signal }
  );

  return readResponse(res);
}

// =======================================
// WAIT FOR PROCESSING RESULTS
// =======================================

export async function waitForJob(
  jobId,
  onStatus = () => {},
  signal,
  interval = 1000
) {
  while (true) {
    if (signal?.aborted) {
      throw new DOMException("Cancelled", "AbortError");
    }

    const job = await getJobStatus(jobId, signal);

    onStatus(job.status);

    if (job.status === "completed") {
      const result = job.result || {};

      return {
        ...result,
        annotated_image: mediaUrl(result.annotated_image),
        processed_video: mediaUrl(result.processed_video),
      };
    }

    if (job.status === "failed") {
      throw new Error(job.error || "Processing failed");
    }

    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      }, interval);

      function onAbort() {
        clearTimeout(timer);
        reject(new DOMException("Cancelled", "AbortError"));
      }

      signal?.addEventListener("abort", onAbort, { once: true });

      if (signal?.aborted) onAbort();
    });
  }
}
