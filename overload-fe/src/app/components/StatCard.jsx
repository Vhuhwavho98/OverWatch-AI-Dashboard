"use client";

export default function StatCard({ title, value, color }) {
  return (
    <div
      style={{
        background: "#1e293b",
        borderTop: `5px solid ${color}`,
        padding: "20px",
        borderRadius: "12px",
        color: "white",
      }}
    >
      <h4>{title}</h4>
      <h1>{value}</h1>
    </div>
  );
}