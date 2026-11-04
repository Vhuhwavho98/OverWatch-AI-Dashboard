import { BrowserRouter, Routes, Route } from "react-router-dom";

import Navbar from "./components/Navbar";

import Dashboard from "./pages/Dashboard";
import Alerts from "./pages/Alerts";
import History from "./pages/History";
import Sidebar from "./components/Sidebar";

function Placeholder({ title }) {
  return (
    <div className="bg-white rounded-3xl shadow p-8">
      <h1 className="text-3xl font-bold">{title}</h1>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>

      <div className="flex bg-slate-100">

        <Sidebar />

        <main className="flex-1 p-5 space-y-5">

          {/* <Navbar /> */}

          <Routes>

            <Route path="/" element={<Dashboard />} />

            <Route
              path="/camera"
              element={<Dashboard />}
            />

            <Route
              path="/image"
              element={<Dashboard />}
            />

            <Route
              path="/video"
              element={<Dashboard />}
            />

            <Route
              path="/alerts"
              element={<Alerts />}
            />

            <Route
              path="/history"
              element={<History />}
            />

            <Route
              path="/settings"
              element={<Placeholder title="Settings" />}
            />

          </Routes>

        </main>

      </div>

    </BrowserRouter>
  );
}