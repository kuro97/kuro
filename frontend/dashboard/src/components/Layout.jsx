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
        <div className="logo">KuroTrack</div>
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
