import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function DashboardPage() {
  const [stats, setStats] = useState(null);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(localStorage.getItem("kt_project") || "");

  useEffect(() => {
    api.getProjects().then((p) => {
      setProjects(p);
      if (!projectId && p.length > 0) {
        setProjectId(p[0].id);
        localStorage.setItem("kt_project", p[0].id);
      }
    });
  }, []);

  useEffect(() => {
    if (projectId) {
      api.getCallStats(projectId, 30).then(setStats);
    }
  }, [projectId]);

  function selectProject(id) {
    setProjectId(id);
    localStorage.setItem("kt_project", id);
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Dashboard</h1>
        {projects.length > 1 && (
          <select value={projectId} onChange={(e) => selectProject(e.target.value)}>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        )}
      </div>

      {stats ? (
        <div className="stats-grid">
          <div className="stat-card">
            <div className="label">Total Calls</div>
            <div className="value">{stats.total_calls}</div>
          </div>
          <div className="stat-card">
            <div className="label">Answered</div>
            <div className="value green">{stats.answered_calls}</div>
          </div>
          <div className="stat-card">
            <div className="label">Missed</div>
            <div className="value red">{stats.missed_calls}</div>
          </div>
          <div className="stat-card">
            <div className="label">Unique Calls</div>
            <div className="value">{stats.unique_calls}</div>
          </div>
          <div className="stat-card">
            <div className="label">Target Calls</div>
            <div className="value">{stats.target_calls}</div>
          </div>
          <div className="stat-card">
            <div className="label">Answer Rate</div>
            <div className="value green">{stats.answer_rate}%</div>
          </div>
          <div className="stat-card">
            <div className="label">Avg Duration</div>
            <div className="value">{stats.avg_duration}s</div>
          </div>
        </div>
      ) : (
        <p style={{ color: "var(--text-dim)" }}>
          {projects.length === 0
            ? "No projects yet. Create one in Projects."
            : "Loading..."}
        </p>
      )}
    </div>
  );
}
