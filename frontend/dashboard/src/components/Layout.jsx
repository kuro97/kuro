import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import {
  LayoutDashboard,
  Phone,
  Hash,
  FolderOpen,
  LogOut,
  User,
} from "lucide-react";
import { cn } from "../lib/utils";
import { Separator } from "./ui/separator";
import { Button } from "./ui/button";

// Пункты навигации с иконками lucide
const navItems = [
  { to: "/", label: "Дашборд", icon: LayoutDashboard, end: true },
  { to: "/calls", label: "Звонки", icon: Phone, end: false },
  { to: "/numbers", label: "Номера", icon: Hash, end: false },
  { to: "/projects", label: "Проекты", icon: FolderOpen, end: false },
];

export default function Layout() {
  const navigate = useNavigate();

  function logout() {
    localStorage.removeItem("kt_token");
    localStorage.removeItem("kt_project");
    navigate("/login");
  }

  return (
    <div className="flex min-h-screen bg-background">
      {/* Левый сайдбар */}
      <aside className="w-60 shrink-0 flex flex-col border-r border-border bg-card">
        {/* Лого и название */}
        <div className="flex flex-col items-start gap-2 px-4 py-5">
          <img
            src="/dashboard/baigelenov-logo.jpg"
            alt="BAIGELENOV"
            className="h-9 rounded bg-white px-1.5 py-0.5 object-contain"
          />
          <span className="text-xs font-medium tracking-wide text-muted-foreground px-0.5">
            KuroTrack
          </span>
        </div>

        <Separator />

        {/* Навигационные ссылки */}
        <nav className="flex flex-col gap-1 px-3 py-3 flex-1">
          {navItems.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
                )
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>

        <Separator />

        {/* Нижняя часть — пользователь и выход */}
        <div className="px-3 py-3 flex flex-col gap-1">
          <div className="flex items-center gap-3 px-3 py-2 text-sm text-muted-foreground">
            <User size={16} />
            <span>Admin</span>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={logout}
            className="justify-start gap-3 text-muted-foreground hover:text-foreground px-3"
          >
            <LogOut size={16} />
            Выйти
          </Button>
        </div>
      </aside>

      {/* Основной контент */}
      <main className="flex-1 overflow-y-auto p-8">
        <Outlet />
      </main>
    </div>
  );
}
