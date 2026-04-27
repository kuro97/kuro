import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

export default function Layout() {
  const navigate = useNavigate();

  function logout() {
    localStorage.removeItem("kt_token");
    localStorage.removeItem("kt_project");
    navigate("/login");
  }

  return (
    <div className="layout">
      <nav className="sidebar">
        {/* Логотип BAIGELENOV — белый фон лого на тёмной теме */}
        <div className="logo" style={{ display: "flex", flexDirection: "column", alignItems: "flex-start", gap: "8px" }}>
          <img
            src="/dashboard/baigelenov-logo.jpg"
            alt="BAIGELENOV"
            style={{
              height: "36px",
              borderRadius: "6px",
              padding: "3px 6px",
              background: "#fff",
            }}
          />
          <span style={{ fontSize: "12px", fontWeight: 500, color: "var(--text-secondary)", letterSpacing: "0.5px" }}>
            KuroTrack
          </span>
        </div>
        <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
          Dashboard
        </NavLink>
        <NavLink to="/calls" className={({ isActive }) => (isActive ? "active" : "")}>
          Calls
        </NavLink>
        <NavLink to="/numbers" className={({ isActive }) => (isActive ? "active" : "")}>
          Numbers
        </NavLink>
        <NavLink to="/projects" className={({ isActive }) => (isActive ? "active" : "")}>
          Projects
        </NavLink>
        <a
          href="#"
          onClick={(e) => {
            e.preventDefault();
            logout();
          }}
          style={{ marginTop: "auto" }}
        >
          Logout
        </a>
      </nav>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
