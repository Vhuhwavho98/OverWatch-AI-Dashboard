import "./globals.css";
import Sidebar from "./components/Sidebar";

export const metadata = {
  title: "OverWatch AI",
  description: "YOLOv11 Vehicle Overload Detection Dashboard",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body className="bg-slate-950 text-white antialiased">
        {/* Top Navigation */}
        <Sidebar />

        {/* Main Dashboard Content */}
        <main className="min-h-[calc(100vh-80px)] bg-slate-950 p-6">
          <div className="mx-auto w-full max-w-[1800px]">
            {children}
          </div>
        </main>
      </body>
    </html>
  );
}