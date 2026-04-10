import React, { useEffect, useState } from "react";
import { api } from "../api";

const COLORS = ["#6366f1", "#22c55e", "#eab308", "#ef4444", "#06b6d4", "#f97316", "#8b5cf6", "#ec4899"];

function SimpleBarChart({ data }) {
  if (!data || data.length === 0) return null;
  const max = Math.max(...data.map((d) => d.total), 1);

  return (
    <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 120 }}>
      {data.map((d, i) => (
        <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center" }}>
          <div
            style={{
              width: "100%",
              maxWidth: 32,
              background: "var(--accent)",
              borderRadius: "4px 4px 0 0",
              height: `${(d.total / max) * 100}px`,
              minHeight: 2,
              position: "relative",
            }}
            title={`${d.date}: ${d.total} calls (${d.answered} answered)`}
          >
            <div
              style={{
                position: "absolute",
                bottom: 0,
                width: "100%",
                height: `${(d.answered / (d.total || 1)) * 100}%`,
                background: "var(--green)",
                borderRadius: "0 0 0 0",
                opacity: 0.7,
              }}
            />
          </div>
          {data.length <= 14 && (
            <div style={{ fontSize: 10, color: "var(--text-dim)", marginTop: 4 }}>
              {d.date.slice(5)}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function SourcesTable({ data }) {
  if (!data || data.length === 0) return <p style={{ color: "var(--text-dim)" }}>No data</p>;
  const total = data.reduce((s, d) => s + d.total, 0);

  return (
    <table style={{ width: "100%", fontSize: 13 }}>
      <tbody>
        {data.map((d, i) => (
          <tr key={d.source}>
            <td style={{ padding: "6px 0" }}>
              <span
                style={{
                  display: "inline-block",
                  width: 10,
                  height: 10,
                  borderRadius: 2,
                  background: COLORS[i % COLORS.length],
                  marginRight: 8,
                }}
              />
              {d.source}
            </td>
            <td style={{ textAlign: "right", fontWeight: 600 }}>{d.total}</td>
            <td style={{ textAlign: "right", color: "var(--text-dim)", width: 60 }}>
              {total > 0 ? Math.round((d.total / total) * 100) : 0}%
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function DashboardPage() {
  const [stats, setStats] = useState(null);
  const [daily, setDaily] = useState([]);
  const [sources, setSources] = useState([]);
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
      api.getDailyChart(projectId, 30).then(setDaily);
      api.getSourcesChart(projectId, 30).then(setSources);
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
        <>
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

          {/* Charts */}
          <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16 }}>
            <div className="stat-card">
              <div className="label" style={{ marginBottom: 16 }}>Calls by Day (30 days)</div>
              <SimpleBarChart data={daily} />
            </div>
            <div className="stat-card">
              <div className="label" style={{ marginBottom: 16 }}>Sources</div>
              <SourcesTable data={sources} />
            </div>
          </div>
        </>
      ) : (
        <p style={{ color: "var(--text-dim)" }}>
          {projects.length === 0 ? "No projects yet. Create one in Projects." : "Loading..."}
        </p>
      )}
    </div>
  );
}
