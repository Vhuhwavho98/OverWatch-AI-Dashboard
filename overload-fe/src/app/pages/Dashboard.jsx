"use client";



import { useEffect, useRef, useState } from "react";

import {

  Camera,

  Upload,

  Video,

  MapPin,

  Bell,

  Truck,

  Car,

  Bus,

  Bike,

  ShieldAlert,

  Activity,

  RefreshCw,

  Image as ImageIcon,
  Eye,
  X,
  ExternalLink,

} from "lucide-react";



import {

  cameraFeed,

  getDashboard,

  getLatestDetection,

  getDetections,

  getAlerts,

  uploadImage,

  uploadVideo,
  waitForJob,
  mediaUrl,

  updateLocation,

} from "../services/api";



const API_URL = "http://127.0.0.1:8000";



// Helper to safely format asset URLs without double slashes

const getAssetUrl = (path) => {

  if (!path) return "";
  if (path.startsWith("blob:")) return path;

  if (path.startsWith("http://") || path.startsWith("https://")) return path;

  const cleanPath = path.startsWith("/") ? path : `/${path}`;

  return `${API_URL}${cleanPath}`;

};


// Keep backend status values consistent in the dashboard.
const normaliseStatus = (value) => {
  const status = String(value || "NORMAL").trim().toUpperCase();

  if (["OVERLOAD", "OVERLOADED", "WARNING", "HIGH", "CRITICAL"].includes(status)) {
    return { label: "OVERLOAD", className: "bg-red-600 text-white" };
  }

  if (["REVIEW_REQUIRED", "REVIEW", "INDETERMINATE", "UNKNOWN"].includes(status)) {
    return { label: "REVIEW REQUIRED", className: "bg-amber-500 text-slate-950" };
  }

  return { label: "NORMAL", className: "bg-green-600 text-white" };
};



export default function Dashboard() {

  // ==========================================================

  // STATE & REFS

  // ==========================================================



  const imageInput = useRef(null);

  const videoInput = useRef(null);
  const activeJob = useRef(null);
  const localMediaUrl = useRef(null);
  const mounted = useRef(true);
  const cameraRetryTimer = useRef(null);
  const cameraRetryCount = useRef(0);

  // Declare state before effects/callbacks that read it.
  const [mode, setMode] = useState("camera");
  const [preview, setPreview] = useState(cameraFeed);
  const [cameraStatus, setCameraStatus] = useState("connecting");

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      activeJob.current?.abort();
      if (cameraRetryTimer.current) clearTimeout(cameraRetryTimer.current);
      if (localMediaUrl.current) URL.revokeObjectURL(localMediaUrl.current);
    };
  }, []);

  // Restart the MJPEG connection if the browser reports that the stream ended.
  // Backoff prevents rapid reconnect loops when the camera/backend is unavailable.
  const reconnectCamera = () => {
    if (!mounted.current || mode !== "camera") return;
    if (cameraRetryTimer.current) clearTimeout(cameraRetryTimer.current);
    cameraRetryCount.current += 1;
    const delay = Math.min(1000 * 2 ** (cameraRetryCount.current - 1), 10000);
    setCameraStatus("reconnecting");
    cameraRetryTimer.current = setTimeout(() => {
      if (mounted.current && mode === "camera") {
        setPreview(`${cameraFeed}${cameraFeed.includes("?") ? "&" : "?"}t=${Date.now()}`);
      }
    }, delay);
  };

  const handleCameraLoad = () => {
    cameraRetryCount.current = 0;
    setCameraStatus("connected");
  };

  const handleCameraError = () => {
    setCameraStatus("disconnected");
    reconnectCamera();
  };

  // Browsers may suspend image streams while a tab is in the background.
  // Reopen the stream when the dashboard becomes visible again.
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === "visible" && mode === "camera") {
        if (cameraRetryTimer.current) clearTimeout(cameraRetryTimer.current);
        setCameraStatus("reconnecting");
        setPreview(`${cameraFeed}${cameraFeed.includes("?") ? "&" : "?"}t=${Date.now()}`);
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, [mode]);

  const cancelPreviousJob = () => {
    activeJob.current?.abort();
    const controller = new AbortController();
    activeJob.current = controller;
    if (localMediaUrl.current) URL.revokeObjectURL(localMediaUrl.current);
    localMediaUrl.current = null;
    return controller;
  };



  const [dashboard, setDashboard] = useState({

    total_vehicles: 0,

    cars: 0,

    trucks: 0,

    buses: 0,

    motorcycles: 0,

    overloaded: 0,

    warnings: 0,

  });



  const [imagePreview, setImagePreview] = useState("");

  const [loadingImage, setLoadingImage] = useState(false);



  const [videoPreview, setVideoPreview] = useState("");

  const [processingVideo, setProcessingVideo] = useState(false);
  const [vehicles, setVehicles] = useState([]);
  const [jobStatus, setJobStatus] = useState("idle")
 const [videoProgress, setVideoProgress] = useState(0);
  const [jobError, setJobError] = useState("");



  const [location, setLocation] = useState({

    address: " Block K, 2 Aubrey Matlala Street, Soshanguve Pretoria",

    latitude: null,

    longitude: null,

  });



  const [latest, setLatest] = useState(null);

  const [history, setHistory] = useState([]);
  const [selectedVehicle, setSelectedVehicle] = useState(null);

  const [alerts, setAlerts] = useState([]);


  useEffect(() => {
    if (!navigator.geolocation) {
      setLocation({
        address: "GPS is not supported by this browser",
        latitude: null,
        longitude: null,
      });
      return;
    }

    navigator.geolocation.getCurrentPosition(
      async ({ coords }) => {
        const gps = {
          latitude: coords.latitude,
          longitude: coords.longitude,
          address: " Block K, 2 Aubrey Matlala Street, Soshanguve Pretoria",
        };

        // Display coordinates immediately.
        setLocation(gps);

        try {
          // FastAPI resolves GPS coordinates into a real address.
          const resolved = await updateLocation(gps);

          setLocation({
            latitude: coords.latitude,
            longitude: coords.longitude,
            address:
              resolved?.address ||
              `GPS: ${coords.latitude.toFixed(6)}, ${coords.longitude.toFixed(6)}`,
          });
        } catch (err) {
          console.error("GPS Update Error:", err);

          setLocation({
            ...gps,
            address: `GPS: ${coords.latitude.toFixed(6)}, ${coords.longitude.toFixed(6)}`,
          });
        }
      },
      (error) => {
        console.error("GPS permission/location error:", error);

        setLocation({
          address: "Unable to obtain GPS location",
          latitude: null,
          longitude: null,
        });
      },
      {
        enableHighAccuracy: true,
        timeout: 15000,
        maximumAge: 60000,
      }
    );
  }, []);


  const loadDashboard = async () => {

    try {

      const [statsRes, latestRes, detectionsRes, alertsRes] = await Promise.all([

        getDashboard(),

        getLatestDetection(),

        getDetections(),

        getAlerts(),

      ]);



      setDashboard(statsRes || {});

      setLatest(latestRes || null);

      setHistory(Array.isArray(detectionsRes) ? detectionsRes : []);

      setAlerts(Array.isArray(alertsRes) ? alertsRes : []);

    } catch (error) {

      console.error("Dashboard Load Error:", error);

    }

  };



  useEffect(() => {

    loadDashboard();

    const interval = setInterval(loadDashboard, 50000);

    return () => clearInterval(interval);

  }, []);



  // ==========================================================

  // IMAGE UPLOAD
  // ==========================================================

  const handleImageUpload = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    const controller = cancelPreviousJob();
    // Show the selected image immediately while the backend analyses it.
    const localPreview = URL.createObjectURL(file);
    localMediaUrl.current = localPreview;
    setMode("image");
    setImagePreview(localPreview);
    setVehicles([]);
    setJobError("");
    setJobStatus("uploading");
    setLoadingImage(true);
    setProcessingVideo(false);

    try {
      const upload = await uploadImage(file, location.address);
      if (controller.signal.aborted || !mounted.current) return;
      setLoadingImage(false);
      setJobStatus(upload.status);

      const result = await waitForJob(
        upload.job_id,
        (status) => {
          if (!controller.signal.aborted && mounted.current) setJobStatus(status);
        },
        controller.signal
      );

      if (controller.signal.aborted || !mounted.current) return;
      if (result.annotated_image) setImagePreview(mediaUrl(result.annotated_image));
      setVehicles(result.vehicles || []);
      setJobStatus("completed");
      await loadDashboard();
    } catch (err) {
      if (controller.signal.aborted || !mounted.current) return;
      console.error("Image analysis failed:", err);
      setJobError(err.message || "Image analysis failed");
      setJobStatus("failed");
    } finally {
      if (activeJob.current === controller && mounted.current) setLoadingImage(false);
    }
  };

  // ==========================================================
  // VIDEO UPLOAD
  // ==========================================================

  const handleVideoUpload = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    const controller = cancelPreviousJob();
    setMode("video");
    setVideoPreview("");
    setVideoProgress(0);
    setVehicles([]);
    setJobError("");
    setJobStatus("uploading");
    setLoadingImage(false);
    setProcessingVideo(true);

    try {
      const upload = await uploadVideo(file, location.address);
      if (controller.signal.aborted || !mounted.current) return;
      setJobStatus(upload.status);

      // Poll the job so the dashboard can begin playback as soon as the
      // backend has finalized the annotated first-quarter preview.
      let result = null;
      while (!controller.signal.aborted && mounted.current) {
        const response = await fetch(`${API_URL}/jobs/${upload.job_id}`, {
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) {
          throw new Error(`Unable to read processing status (${response.status})`);
        }

        const job = await response.json();
        setJobStatus(job.status || "processing");
        setVideoProgress(Number(job.progress || 0));

        if (job.preview_video) {
          const nextPreview = mediaUrl(job.preview_video);
          setVideoPreview((current) =>
            current.startsWith(nextPreview) ? current : `${nextPreview}?v=${Date.now()}`
          );
        }

        if (job.status === "completed") {
          result = job.result || {};
          break;
        }
        if (job.status === "failed") {
          throw new Error(job.error || "Video processing failed");
        }

        await new Promise((resolve) => setTimeout(resolve, 1000));
      }

      if (controller.signal.aborted || !mounted.current || !result) return;
      if (result.processed_video) {
        const finalVideo = mediaUrl(result.processed_video);
        setVideoPreview(`${finalVideo}?v=${Date.now()}`);
      }
      setVehicles(result.vehicles || []);
      setVideoProgress(100);
      setJobStatus("completed");
      await loadDashboard();
    } catch (err) {
      if (controller.signal.aborted || !mounted.current) return;
      console.error("Video analysis failed:", err);
      setJobError(err.message || "Video analysis failed");
      setJobStatus("failed");
    } finally {
      if (activeJob.current === controller && mounted.current) setProcessingVideo(false);
    }
  };

  // ==========================================================
  // SWITCH TO LIVE CAMERA

  // ==========================================================

  const openCamera = () => {
    cancelPreviousJob();
    setLoadingImage(false);
    setProcessingVideo(false);
    setJobStatus("idle");
    setJobError("");
    setMode("camera");
    cameraRetryCount.current = 0;
    if (cameraRetryTimer.current) clearTimeout(cameraRetryTimer.current);
    setCameraStatus("connecting");
    setPreview(`${cameraFeed}${cameraFeed.includes("?") ? "&" : "?"}t=${Date.now()}`);

  };



  // ==========================================================

  // DASHBOARD STAT CARDS

  // ==========================================================

  const statCards = [

    { title: "Total Vehicles", value: dashboard.total_vehicles, icon: Truck, color: "bg-green-600" },

    { title: "Cars", value: dashboard.cars, icon: Car, color: "bg-blue-600" },

    { title: "Trucks", value: dashboard.trucks, icon: Truck, color: "bg-orange-500" },

    { title: "Buses", value: dashboard.buses, icon: Bus, color: "bg-purple-600" },

    { title: "Motorcycles", value: dashboard.motorcycle, icon: Bike, color: "bg-cyan-600" },

    { title: "Warnings", value: dashboard.warnings, icon: Bell, color: "bg-yellow-500" },

    { title: "Overloaded", value: dashboard.overloaded, icon: ShieldAlert, color: "bg-red-600" },

    { title: "Live System", value: "ACTIVE", icon: Activity, color: "bg-emerald-600" },

  ];



  // ==========================================================

  // UI

  // ==========================================================

  return (

    <div className="min-h-screen bg-slate-950 text-white p-6">

      {/* HEADER */}

      <section className="mb-8 flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">

        <div>

          <h1 className="text-4xl font-bold text-white">Vehicle Overload Detection Dashboard</h1>

          <p className="mt-2 text-slate-400">YOLOv11 • OCR • ByteTrack • CSV Records • FastAPI</p>

        </div>



        <button

          onClick={loadDashboard}

          className="flex items-center gap-2 rounded-xl bg-green-600 px-5 py-3 font-semibold transition hover:bg-green-700"

        >

          <RefreshCw size={18} />

          Refresh Dashboard

        </button>

      </section>



      {/* STATISTICS */}

      <section className="mb-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">

        {statCards.map((card, index) => {

          const Icon = card.icon;

          return (

            <div key={index} className="rounded-2xl border border-slate-800 bg-slate-900 p-5 shadow-lg">

              <div className="flex items-center justify-between">

                <div>

                  <p className="text-sm text-slate-400">{card.title}</p>

                  <h2 className="mt-2 text-3xl font-bold">{card.value}</h2>

                </div>

                <div className={`rounded-xl p-3 ${card.color}`}>

                  <Icon size={26} className="text-white" />

                </div>

              </div>

            </div>

          );

        })}

      </section>



      {/* CAMERA & CONTROLS */}

      <section className="mb-8 grid gap-6 lg:grid-cols-[2fr_1fr]">

        <div className="rounded-2xl border border-slate-800 bg-slate-900 shadow-lg">

          <div className="flex items-center justify-between border-b border-slate-800 p-5">

            <div>

              <h2 className="text-xl font-semibold">Live Detection Feed</h2>

              <p className="text-sm text-slate-400">Vehicle Detection ({mode.toUpperCase()}) • {jobStatus === "completed" ? `${vehicles.length} vehicle(s)` : jobStatus}</p>

            </div>

            <div className="flex items-center gap-2 rounded-full bg-green-500/10 px-3 py-2 text-green-400">

              <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-green-400"></span>

              {cameraStatus === "connected" ? "LIVE" : cameraStatus.toUpperCase()}

            </div>

          </div>

          <div className="relative w-full h-[520px] bg-black rounded-b-2xl overflow-hidden">



            {/* Camera */}

            {mode === "camera" && (

              <img

                key={preview}

                src={preview}

                alt="Live Camera"

                onLoad={handleCameraLoad}

                onError={handleCameraError}

                className="w-full h-full object-contain"

              />

            )}



            {/* Uploaded Image */}

            {mode === "image" && imagePreview && (

              <img

                src={imagePreview}

                alt="Processed Image"

                className="w-full h-full object-contain"

              />

            )}



            {/* Uploaded Video */}

            {mode === "video" && videoPreview && (

              <video

                key={videoPreview}

                src={videoPreview}

                className="w-full h-full object-contain"

                controls

                autoPlay

                muted

                playsInline

              />

            )}



            {/* Non-blocking status: preview remains visible */}
            {mode === "video" && ["uploading", "queued", "processing"].includes(jobStatus) && (
              <div className="absolute top-3 right-3 pointer-events-none rounded-lg bg-slate-950/90 px-4 py-3 text-sm text-green-300 shadow">
                <div>{videoPreview ? "Annotated preview available" : "Processing video before playback..."}</div>
                <div className="mt-1">{videoProgress}% processed</div>
                <div className="mt-2 h-1.5 w-48 overflow-hidden rounded-full bg-slate-700">
                  <div className="h-full rounded-full bg-green-400 transition-all" style={{ width: `${videoProgress}%` }} />
                </div>
                {!videoPreview && <div className="mt-1 text-xs text-slate-300">Playback starts at 25% with bounding boxes.</div>}
              </div>
            )}
            {mode === "image" && ["uploading", "queued", "processing"].includes(jobStatus) && (
              <div className="absolute top-3 right-3 pointer-events-none rounded-lg bg-slate-950/85 px-4 py-2 text-sm text-green-300 shadow">
                {jobStatus === "uploading" ? "Uploading image..." : "Analysing image..."}
              </div>
            )}
            {mode !== "camera" && jobError && (
              <div role="alert" className="absolute bottom-3 left-3 right-3 rounded-lg bg-red-950/90 p-3 text-sm text-red-200">
                {jobError}
              </div>
            )}
          </div>
        </div>

        {/* CONTROL PANEL */}

        <div className="space-y-5">

          <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 shadow-lg">

            <h2 className="mb-5 text-lg font-semibold">Detection Controls</h2>



            <div className="space-y-4">

              <button

                onClick={openCamera}

                className="flex w-full items-center justify-center gap-2 rounded-xl bg-green-600 px-4 py-3 font-semibold hover:bg-green-700"

              >

                <Camera size={20} />

                Live Camera

              </button>



              <button

                onClick={() => imageInput.current?.click()}

                disabled={loadingImage}

                className="flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-4 py-3 font-semibold hover:bg-blue-700 disabled:opacity-50"

              >

                <ImageIcon size={20} />

                {loadingImage ? "Uploading..." : "Upload Image"}

              </button>



              <button

                onClick={() => videoInput.current?.click()}

                disabled={processingVideo}

                className="flex w-full items-center justify-center gap-2 rounded-xl bg-purple-600 px-4 py-3 font-semibold hover:bg-purple-700 disabled:opacity-50 text-white"

              >

                <Video size={20} />

                {processingVideo ? "Processing Video..." : "Upload Video"}

              </button>



              <input

                ref={imageInput}

                hidden

                type="file"

                accept="image/*"

                onChange={handleImageUpload}

              />



              <input

                ref={videoInput}

                hidden

                type="file"

                accept="video/*"

                onChange={handleVideoUpload}

              />

            </div>

          </div>



          {/* GPS */}

          <div className="rounded-2xl border border-slate-800 bg-slate-900 p-5 shadow-lg">

            <div className="mb-4 flex items-center gap-2 text-green-400">

              <MapPin size={20} />

              <h2 className="font-semibold">Current GPS Location</h2>

            </div>

            <div className="space-y-3 text-sm">

              <div>

                <p className="text-slate-500">Address</p>

                <p>{location.address}</p>

              </div>

              <div className="grid grid-cols-2 gap-3">

                <div>

                  <p className="text-slate-500">Latitude</p>

                  <p>{location.latitude || "--"}</p>

                </div>

                <div>

                  <p className="text-slate-500">Longitude</p>

                  <p>{location.longitude || "--"}</p>

                </div>

              </div>

            </div>

          </div>

        </div>

      </section>



      {/* LIVE VEHICLE DETECTIONS TABLE */}

      <section className="mb-8 rounded-2xl border border-slate-800 bg-slate-900 p-6 shadow-lg">

        <div className="mb-5 flex items-center justify-between">

          <div>

            <h2 className="text-xl font-semibold">Detection History</h2>

            <p className="text-sm text-slate-400">Detection records loaded from detections.csv</p>

          </div>

          <span className="rounded-full bg-blue-500/20 px-3 py-1 text-sm text-blue-400">

            {history.length} Records

          </span>

        </div>



        <div className="max-h-[450px] overflow-auto rounded-xl border border-slate-800">

          <table className="min-w-full divide-y divide-slate-800 text-left text-sm text-gray-300">

            <thead className="sticky top-0 bg-slate-950 text-cyan-400 uppercase text-xs">

              <tr>

                <th className="px-4 py-3">Tracker ID</th>

                <th className="px-4 py-3">Vehicle</th>

                <th className="px-4 py-3">Registration</th>

                <th className="px-4 py-3">Confidence</th>

                <th className="px-4 py-3">Status</th>

                <th className="px-4 py-3">Location</th>

                <th className="px-4 py-3">Time</th>

                <th className="px-4 py-3 text-center">Snapshots</th>

                <th className="px-4 py-3 text-center">Action</th>

              </tr>

            </thead>

            <tbody className="divide-y divide-slate-800">

              {history.length === 0 ? (

                <tr>

                  <td colSpan={9} className="py-8 text-center text-gray-400">No detections found.</td>

                </tr>

              ) : (

                history.map((item, index) => (

                  <tr key={item.id ?? index} className="hover:bg-slate-800 transition-colors">

                    <td className="px-4 py-3 font-medium text-white">
                      {item.tracker_id !== null && item.tracker_id !== undefined
                        ? `#${item.tracker_id}`
                        : "Not recorded"}
                    </td>

                    <td className="px-4 py-3 capitalize">{item.vehicle_type || "Unknown"}</td>

                    <td className="px-4 py-3 font-mono text-cyan-300">{item.registration_number || "N/A"}</td>

                    <td className="px-4 py-3">{item.confidence ? `${Number(item.confidence).toFixed(2)}%` : "0%"}</td>

                    <td className="px-4 py-3">

                      {(() => {
                        const status = normaliseStatus(item.overload_status);
                        return (
                          <span className={`px-2 py-1 rounded-full text-xs font-semibold ${status.className}`}>
                            {status.label}
                          </span>
                        );
                      })()}

                    </td>

                    <td className="px-4 py-3 truncate max-w-[150px]">{item.location || "Unknown"}</td>

                    <td className="px-4 py-3 whitespace-nowrap text-gray-400">

                      {item.timestamp ? new Date(item.timestamp).toLocaleString("en-ZA") : "N/A"}

                    </td>

                    <td className="px-4 py-3 text-center">
                      {(() => {
                        const snapshotPath = item.snapshot;
                        return snapshotPath ? (
                          <img
                            src={getAssetUrl(snapshotPath)}
                            alt={`Snapshot for tracker ${item.tracker_id ?? item.id ?? "vehicle"}`}
                            className="mx-auto h-12 w-20 rounded-md border border-slate-600 object-cover shadow"
                            loading="lazy"
                          />
                        ) : (
                          <span className="text-gray-500">—</span>
                        );
                      })()}
                    </td>

                    <td className="px-4 py-3 text-center">
                      <button
                        type="button"
                        onClick={() => setSelectedVehicle({
                          ...item,
                          snapshot: item.snapshot || null,
                        })}
                        className="inline-flex items-center gap-2 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-3 py-2 text-xs font-semibold text-cyan-300 transition hover:border-cyan-400 hover:bg-cyan-500/20 focus:outline-none focus:ring-2 focus:ring-cyan-400"
                      >
                        <Eye size={15} />
                        View
                      </button>
                    </td>

                  </tr>

                ))

              )}

            </tbody>

          </table>

        </div>

      </section>

      {/* FULL-SIZE VEHICLE SNAPSHOT MODAL */}
      {selectedVehicle && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4 backdrop-blur-sm"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setSelectedVehicle(null);
          }}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="vehicle-snapshot-title"
            className="relative flex max-h-[95vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-slate-700 bg-slate-900 shadow-2xl"
          >
            <header className="flex items-center justify-between border-b border-slate-700 px-5 py-4">
              <div>
                <h2 id="vehicle-snapshot-title" className="text-lg font-bold text-white">Summary</h2>
                <p className="text-xs text-slate-400">Tracker #{selectedVehicle.tracker_id ?? selectedVehicle.id ?? "N/A"}</p>
              </div>
              <button
                type="button"
                onClick={() => setSelectedVehicle(null)}
                aria-label="Close vehicle snapshot"
                className="rounded-lg p-2 text-slate-300 transition hover:bg-slate-800 hover:text-white focus:outline-none focus:ring-2 focus:ring-cyan-400"
              >
                <X size={21} />
              </button>
            </header>

            <div className="grid min-h-0 flex-1 gap-5 overflow-y-auto p-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(260px,0.8fr)]">
              <div className="flex min-h-[240px] items-center justify-center overflow-hidden rounded-xl border border-slate-700 bg-black">
                {selectedVehicle.snapshot ? (
                  <img
                    src={getAssetUrl(selectedVehicle.snapshot)}
                    alt={`Full vehicle snapshot for tracker ${selectedVehicle.tracker_id ?? selectedVehicle.id ?? "unknown"}`}
                    className="max-h-[70vh] w-full object-contain"
                  />
                ) : (
                  <div className="p-8 text-center text-slate-400">
                    <ImageIcon className="mx-auto mb-3" size={36} />
                    <p>No snapshot is available for this detection.</p>
                  </div>
                )}
              </div>

<div className="space-y-4">
  <h3 className="text-sm font-semibold uppercase tracking-wider text-cyan-300">
    Detection details
  </h3>

  {[
    ["Vehicle", selectedVehicle.vehicle_type || "Unknown"],
    ["Registration", selectedVehicle.registration_number || "UNKNOWN"],
    ["Confidence", `${Number(selectedVehicle.confidence ?? 0).toFixed(2)}%`],
    ["Status", selectedVehicle.overload_status || "NORMAL"],
    ["Location", selectedVehicle.location || "Unknown"],
    [
      "Detected at",
      selectedVehicle.timestamp
        ? new Date(selectedVehicle.timestamp).toLocaleString("en-ZA")
        : "N/A",
    ],
  ].map(([label, value]) => (
    <div key={label} className="border-b border-slate-800 pb-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="mt-1 break-words text-sm text-white">{value}</p>
    </div>
  ))}

  {/* NUMBER PLATE SNIPPET */}
  <div className="space-y-2 border-b border-slate-800 pb-4">
    <h4 className="text-xs font-semibold uppercase tracking-wider text-cyan-300">
      Captured number plate
    </h4>

    {selectedVehicle.plate_snippet ? (
      <>
        <div className="overflow-hidden rounded-lg border border-cyan-500/30 bg-slate-950 p-2">
          <img
            src={getAssetUrl(selectedVehicle.plate_snippet)}
            alt={`Number plate for tracker ${selectedVehicle.tracker_id || ""}`}
            className="max-h-32 w-full object-contain"
            onError={(e) => {
              e.currentTarget.style.display = "none";
            }}
          />
        </div>

        <a
          href={getAssetUrl(selectedVehicle.plate_snippet)}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-2 rounded-lg bg-cyan-600 px-3 py-2 text-xs font-semibold text-white hover:bg-cyan-500"
        >
          <ExternalLink size={14} />
          Open plate snippet
        </a>
      </>
    ) : (
      <p className="rounded-lg bg-slate-900 p-3 text-xs text-amber-400">
        No plate snippet captured yet. Registration requires review.
      </p>
    )}
  </div>

  {/* ORIGINAL VEHICLE SNAPSHOT */}
  {selectedVehicle.snapshot && (
    <a
      href={getAssetUrl(selectedVehicle.snapshot)}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-2 rounded-lg bg-cyan-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-cyan-500"
    >
      <ExternalLink size={15} />
      Open original vehicle image
    </a>
  )}
</div>

            </div>
          </section>
        </div>
      )}

    </div>

  );

}