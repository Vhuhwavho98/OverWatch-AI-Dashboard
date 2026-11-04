"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  LineChart,
  Line,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  Legend,
} from "recharts";

import { BiSolidReport } from "react-icons/bi";
import { getDetections, getDashboard } from "../../services/api";
import "./History.css";

const COLORS = ["#00E396", "#008FFB", "#FEB019", "#FF4560", "#775DD0"];

export default function History() {
  const [detections, setDetections] = useState([]);
  const [dashboard, setDashboard] = useState({
    total_vehicles: 0,
    cars: 0,
    trucks: 0,
    buses: 0,
    motorcycles: 0,
    overloaded: 0,
    warnings: 0,
  });

  useEffect(() => {
    loadReports();
  }, []);

  const loadReports = async () => {
    try {
      const history = await getDetections();
      const stats = await getDashboard();

      setDetections(history);
      setDashboard(stats);
    } catch (error) {
      console.error("Reports Error:", error);
    }
  };

  // ==============================
  // Vehicle Type Pie Chart
  // ==============================
  const vehiclePie = useMemo(() => {
    const counts = {};

    detections.forEach((d) => {
      counts[d.vehicle_type] = (counts[d.vehicle_type] || 0) + 1;
    });

    return Object.entries(counts).map(([name, value]) => ({
      name,
      value,
    }));
  }, [detections]);

  // ==============================
  // Overload Status Bar Chart
  // ==============================
  const overloadBar = useMemo(() => {
    const counts = {};

    detections.forEach((d) => {
      counts[d.overload_status] = (counts[d.overload_status] || 0) + 1;
    });

    return Object.entries(counts).map(([status, total]) => ({
      status,
      total,
    }));
  }, [detections]);

  // ==============================
  // Confidence Trend
  // ==============================
  const confidenceTrend = useMemo(() => {
    return detections
      .slice()
      .reverse()
      .map((d, index) => ({
        detection: index + 1,
        confidence: Number(d.confidence),
      }));
  }, [detections]);

  // ==============================
  // Hourly Detection Trend
  // ==============================
  const hourlyTrend = useMemo(() => {
    const counts = {};

    detections.forEach((d) => {
      const hour = new Date(d.created_at).getHours();
      counts[hour] = (counts[hour] || 0) + 1;
    });

    return Object.keys(counts)
      .sort((a, b) => Number(a) - Number(b))
      .map((hour) => ({
        hour: `${hour}:00`,
        vehicles: counts[hour],
      }));
  }, [detections]);

  return (

    <div className="dashboard">
      <main className="main-content">
    <div className="reports-page">
      <header className="reports-header">
        <h1><BiSolidReport/> Vehicle OverWatch AI Reports</h1>
        <p>Vehicle Overload Detection Analytics Dashboard</p>
      </header>
      <section className="kpi-grid">
        <div className="kpi-card green">
          <small>Total Vehicles</small>
          <h2>{dashboard.total_vehicles}</h2>
        </div>

        <div className="kpi-card blue">
          <small>Cars</small>
          <h2>{dashboard.cars}</h2>
        </div>

        <div className="kpi-card yellow">
          <small>Trucks</small>
          <h2>{dashboard.trucks}</h2>
        </div>

        <div className="kpi-card red">
          <small>High Overloads</small>
          <h2>{dashboard.overloaded}</h2>
        </div>
      </section>

      {/* FIRST ROW */}

      <section className="chart-grid">
        <div className="chart-card">
          <h3>Vehicle Distribution</h3>

          <ResponsiveContainer width="100%" height={300}>
            <PieChart>
              <Pie
                data={vehiclePie}
                dataKey="value"
                nameKey="name"
                outerRadius={100}
                label
              >
                {vehiclePie.map((entry, index) => (
                  <Cell
                    key={index}
                    fill={COLORS[index % COLORS.length]}
                  />
                ))}
              </Pie>

              <Tooltip />
              <Legend />
            </PieChart>
          </ResponsiveContainer>
        </div>

        <div className="chart-card">
          <h3>Overload Status</h3>

          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={overloadBar}>
              <CartesianGrid strokeDasharray="3 3" stroke="#153a69" />
              <XAxis dataKey="status" stroke="#9FB6D0" />
              <YAxis stroke="#9FB6D0" />
              <Tooltip />
              <Bar dataKey="total" fill="#FF4560" radius={[6,6,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      {/* SECOND ROW */}

      <section className="chart-card full-width">
        <h3>Detections Throughout the Day</h3>

        <ResponsiveContainer width="100%" height={330}>
          <LineChart data={hourlyTrend}>
            <CartesianGrid strokeDasharray="3 3" stroke="#153a69" />
            <XAxis dataKey="hour" stroke="#9FB6D0" />
            <YAxis stroke="#9FB6D0" />
            <Tooltip />

            <Line
              type="monotone"
              dataKey="vehicles"
              stroke="#00E396"
              strokeWidth={3}
              dot={{ r: 5 }}
            />
          </LineChart>
        </ResponsiveContainer>
      </section>

      {/* THIRD ROW */}

      <section className="chart-card full-width">
        <h3>YOLO Detection Confidence</h3>

        <ResponsiveContainer width="100%" height={330}>
          <AreaChart data={confidenceTrend}>
            <CartesianGrid strokeDasharray="3 3" stroke="#153a69" />
            <XAxis dataKey="detection" stroke="#9FB6D0" />
            <YAxis stroke="#9FB6D0" />
            <Tooltip />

            <Area
              type="monotone"
              dataKey="confidence"
              stroke="#FEB019"
              fill="#FEB01933"
              strokeWidth={3}
            />
          </AreaChart>
        </ResponsiveContainer>
      </section>

      {/* TABLE */}

      <section className="history-card">
        <div className="history-header">
          <h2>🚨 Detection Report</h2>

          <button onClick={loadReports}>
            Refresh
          </button>
        </div>

        <table className="report-table">
          <thead>
            <tr>
              <th>Date / Time</th>
              <th>Vehicle</th>
              <th>Registration</th>
              <th>Confidence</th>
              <th>Status</th>
              <th>Location</th>
            </tr>
          </thead>

          <tbody>
            {detections.length === 0 ? (
              <tr>
                <td colSpan="6" className="empty">
                  No detections available.
                </td>
              </tr>
            ) : (
              detections.map((item) => (
                <tr key={item.id}>
                  <td>{item.created_at}</td>

                  <td>{item.vehicle_type}</td>

                  <td>{item.registration_number}</td>

                  <td>{Math.round(item.confidence)}%</td>

                  <td>
                    <span
                      className={`status ${item.overload_status.toLowerCase()}`}
                    >
                      {item.overload_status}
                    </span>
                  </td>

                  <td>{item.location}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>
    </div>
    </main>
    </div>
  );
}