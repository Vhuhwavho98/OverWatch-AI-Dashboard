"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Camera,
  ImageIcon,
  Video,
  History,
  Bell,
  Settings,
  Truck,
  ShieldCheck,
} from "lucide-react";

const menuItems = [
  {
    title: "Dashboard",
    href: "/",
    icon: LayoutDashboard,
  },
  {
    title: "Live Camera",
    href: "/camera",
    icon: Camera,
  },
  {
    title: "Image Detection",
    href: "/image",
    icon: ImageIcon,
  },
  {
    title: "Video Detection",
    href: "/video",
    icon: Video,
  },
  {
    title: "Detection History",
    href: "/history",
    icon: History,
  },
  {
    title: "Alerts",
    href: "/alerts",
    icon: Bell,
  },
  {
    title: "Settings",
    href: "/settings",
    icon: Settings,
  },
];

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-50 w-full border-b border-slate-800 bg-[#071827] shadow-lg shadow-black/30">
      <div className="flex h-20 items-center justify-between px-6">

        {/* ================= LOGO ================= */}
        <Link href="/" className="flex items-center gap-3">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-gradient-to-br from-green-500 to-emerald-600 shadow-md shadow-green-500/30">
            <Truck className="text-white" size={24} />
          </div>

          <div className="hidden sm:block">
            <h1 className="text-lg font-bold text-white leading-none">
            </h1>
            <p className="text-xs text-slate-400">
              Vehicle Overload Detection
            </p>
          </div>
        </Link>

        {/* ================= MENU ================= */}
        <nav className="hidden lg:flex items-center gap-2">

          {menuItems.map(({ title, href, icon: Icon }) => {
            const active =
              pathname === href ||
              (href !== "/" && pathname.startsWith(href));

            return (
              <Link
                key={href}
                href={href}
                className={`flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium transition-all duration-200 ${
                  active
                    ? "bg-green-600 text-white shadow-lg shadow-green-500/25"
                    : "text-slate-300 hover:bg-slate-800 hover:text-green-400"
                }`}
              >
                <Icon size={18} />
                <span>{title}</span>
              </Link>
            );
          })}

        </nav>

        {/* ================= RIGHT SECTION ================= */}
        <div className="flex items-center gap-3">

          {/* Live Badge */}
          <div className="flex items-center gap-2 rounded-full border border-green-500/30 bg-green-500/10 px-3 py-2">
            <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-green-400"></span>

            <span className="text-xs font-semibold uppercase tracking-wide text-green-400">
              Live
            </span>
          </div>

          {/* YOLO Badge */}
          <div className="hidden md:flex items-center gap-2 rounded-full border border-cyan-500/30 bg-cyan-500/10 px-3 py-2">
            <ShieldCheck size={16} className="text-cyan-400" />

            <span className="text-xs font-semibold text-cyan-400">
              YOLOv11
            </span>
          </div>

        </div>

      </div>

      {/* ================= MOBILE MENU ================= */}

      <div className="flex gap-2 overflow-x-auto border-t border-slate-800 bg-[#091b2d] px-4 py-3 lg:hidden scrollbar-hide">

        {menuItems.map(({ title, href, icon: Icon }) => {
          const active =
            pathname === href ||
            (href !== "/" && pathname.startsWith(href));

          return (
            <Link
              key={href}
              href={href}
              className={`flex min-w-max items-center gap-2 rounded-full px-4 py-2 text-sm ${
                active
                  ? "bg-green-600 text-white"
                  : "bg-slate-800 text-slate-300"
              }`}
            >
              <Icon size={16} />
              {title}
            </Link>
          );
        })}

      </div>
    </header>
  );
}