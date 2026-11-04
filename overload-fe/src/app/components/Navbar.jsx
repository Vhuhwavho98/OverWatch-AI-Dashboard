import { MapPin, Wifi, Clock } from "lucide-react";
import { useEffect, useState } from "react";

export default function Navbar() {

  const [time, setTime] = useState(new Date());

  useEffect(() => {
    const timer = setInterval(() => {
      setTime(new Date());
    }, 1000);

    return () => clearInterval(timer);
  }, []);

  return (
    <header className="bg-white shadow rounded-2xl p-4 flex justify-between items-center">

      <div>
        <h2 className="font-bold text-2xl text-slate-700">
          Smart Vehicle Overload Detection
        </h2>

        <p className="text-sm text-slate-400">
          YOLOv11 • OCR • ByteTrack • FastAPI
        </p>
      </div>

      <div className="flex gap-5 items-center">

        <div className="flex gap-2 items-center text-green-600">
          <Wifi size={18} />
          Live
        </div>

        <div className="flex gap-2 items-center text-red-600">
          <MapPin size={18} />
          GPS Active
        </div>

        <div className="flex gap-2 items-center text-slate-600">
          <Clock size={18} />
          {time.toLocaleTimeString()}
        </div>

      </div>

    </header>
  );
}