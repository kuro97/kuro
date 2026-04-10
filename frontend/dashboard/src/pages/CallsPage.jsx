import React, { useEffect, useState } from "react";
import { api } from "../api";

function formatDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("ru-KZ", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function formatDuration(sec) {
  if (!sec) return "0:00";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function CallsPage() {
  const [calls, setCalls] = useState([]);
  const [filter, setFilter] = useState({ source: "", disposition: "" });
  const projectId = localStorage.getItem("kt_project");

  useEffect(() => {
    if (projectId) {
      const params = {};
      if (filter.source) params.source = filter.source;
      if (filter.disposition) params.disposition = filter.disposition;
      api.getCalls(projectId, params).then(setCalls);
    }
  }, [projectId, filter]);

  return (
    <div>
      <h1>Calls</h1>

      <div className="form-row" style={{ marginBottom: 16 }}>
        <div className="form-group">
          <label>Source</label>
          <input
            placeholder="google, yandex..."
            value={filter.source}
            onChange={(e) => setFilter({ ...filter, source: e.target.value })}
          />
        </div>
        <div className="form-group">
          <label>Status</label>
          <select
            value={filter.disposition}
            onChange={(e) => setFilter({ ...filter, disposition: e.target.value })}
          >
            <option value="">All</option>
            <option value="ANSWERED">Answered</option>
            <option value="NO ANSWER">Missed</option>
            <option value="BUSY">Busy</option>
          </select>
        </div>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Caller</th>
              <th>Tracking #</th>
              <th>Source</th>
              <th>Campaign</th>
              <th>Duration</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {calls.length === 0 ? (
              <tr>
                <td colSpan="7" style={{ textAlign: "center", color: "var(--text-dim)" }}>
                  No calls yet
                </td>
              </tr>
            ) : (
              calls.map((c) => (
                <tr key={c.id}>
                  <td>{formatDate(c.started_at)}</td>
                  <td>{c.caller_number}</td>
                  <td>{c.tracking_did}</td>
                  <td>{c.source || "direct"}</td>
                  <td>{c.campaign || "-"}</td>
                  <td>{formatDuration(c.billsec)}</td>
                  <td>
                    <span
                      className={`badge ${
                        c.disposition === "ANSWERED"
                          ? "answered"
                          : c.disposition === "BUSY"
                          ? "busy"
                          : "missed"
                      }`}
                    >
                      {c.disposition === "ANSWERED"
                        ? "Answered"
                        : c.disposition === "BUSY"
                        ? "Busy"
                        : "Missed"}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
