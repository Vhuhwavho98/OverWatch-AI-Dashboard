
 // =======================================
 // OVERWATCH AI - API CONFIGURATION
 // =======================================
 // Optional frontend .env.local setting:
 // NEXT_PUBLIC_API_URL=http://127.0.0.1:8000

const API ="https://mega-gaps-locked-dvds.trycloudflare.com"

export const API_BASE_URL = API;
export const cameraFeed = `${API}/video_feed`;

// =======================================
// MEDIA URL HELPER
// =======================================

export function mediaUrl(path) {
  if (!path) return null;

  const value = String(path);

  if (
    value.startsWith("blob:") ||
    value.startsWith("data:") ||
    /^https?:\/\//i.test(value)
  ) {
    return value;
  }

  return `${API}/${value.replace(/^\/+/, "")}`;
}

// =======================================
// RESPONSE HANDLER
// =======================================

async function readResponse(response) {
  const contentType = response.headers.get("content-type") || "";

  const data = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const message =
      typeof data === "object" && data !== null
        ? data.detail || data.error || data.message
        : data;

    throw new Error(message || `Request failed: ${response.status}`);
  }

  return data;
}

// =======================================
// FETCH HELPER
// =======================================

async function apiFetch(endpoint, options = {}) {
  try {
    const response = await fetch(`${API}${endpoint}`, options);
    return await readResponse(response);
  } catch (error) {
    if (error?.name === "AbortError") throw error;

    if (error instanceof TypeError) {
      throw new Error(
        `Cannot connect to OverWatch AI backend at ${API}. ` +
        "Check that FastAPI is running and that the frontend is using the correct backend address."
      );
    }

    throw error;
  }
}

// =======================================
// DASHBOARD
// =======================================

export function getDashboard() {
  return apiFetch("/dashboard");
}

export function getLatestDetection() {
  return apiFetch("/dashboard/latest");
}

export function getDetections() {
  return apiFetch("/detections");
}

export function getAlerts() {
  return apiFetch("/alerts");
}

// =======================================
// JOB STATUS
// =======================================

export function getJobStatus(jobId, signal) {
  if (!jobId) throw new Error("A job ID is required.");

  return apiFetch(`/jobs/${encodeURIComponent(jobId)}`, { signal });
}

// =======================================
// GPS LOCATION
// =======================================

export function updateLocation(location) {
  if (
    !location ||
    typeof location.latitude !== "number" ||
    typeof location.longitude !== "number"
  ) {
    throw new Error("Valid GPS coordinates are required.");
  }

  return apiFetch("/location", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      latitude: location.latitude,
      longitude: location.longitude,
      address: location.address || "",
    }),
  });
}

// =======================================
// IMAGE UPLOAD
// =======================================

export async function uploadImage(file, location) {
  if (!file) throw new Error("Please select an image to upload.");

  const formData = new FormData();
  formData.append("file", file);
  formData.append("location", location || "Polokwane Weighbridge");

  const data = await apiFetch("/upload/image", {
    method: "POST",
    body: formData,
  });

  return {
    ...data,
    preview_image: mediaUrl(data?.preview_image),
  };
}

// =======================================
// VIDEO UPLOAD
// =======================================

export async function uploadVideo(file, location) {
  if (!file) throw new Error("Please select a video to upload.");

  const formData = new FormData();
  formData.append("file", file);
  formData.append("location", location || "Polokwane Weighbridge");

  const data = await apiFetch("/upload/video", {
    method: "POST",
    body: formData,
  });

  return {
    ...data,
    preview_video: mediaUrl(data?.preview_video),
  };
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
  if (!jobId) throw new Error("A processing job ID is required.");

  while (true) {
    if (signal?.aborted) {
      throw new DOMException("Cancelled", "AbortError");
    }

    const job = await getJobStatus(jobId, signal);
    onStatus(job?.status);

    const status = String(job?.status || "").toLowerCase();

    if (status === "completed") {
      const result = job.result || {};

      return {
        ...result,
        annotated_image: mediaUrl(result.annotated_image),
        processed_video: mediaUrl(result.processed_video),
      };
    }

    if (status === "failed" || status === "error") {
      throw new Error(job.error || "Processing failed");
    }

    await new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new DOMException("Cancelled", "AbortError"));
        return;
      }

      let timer;

      const cleanup = () => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
      };

      const onAbort = () => {
        cleanup();
        reject(new DOMException("Cancelled", "AbortError"));
      };

      timer = setTimeout(() => {
        cleanup();
        resolve();
      }, interval);

      signal?.addEventListener("abort", onAbort, { once: true });
    });
  }
}
