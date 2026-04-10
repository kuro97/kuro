import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function NumbersPage() {
  const [numbers, setNumbers] = useState([]);
  const [phone, setPhone] = useState("");
  const [bulkPhones, setBulkPhones] = useState("");
  const [poolStats, setPoolStats] = useState(null);
  const projectId = localStorage.getItem("kt_project");

  function load() {
    if (projectId) {
      api.getNumbers(projectId).then(setNumbers);
    }
  }

  useEffect(load, [projectId]);

  async function addNumber(e) {
    e.preventDefault();
    if (!phone || !projectId) return;
    await api.addNumber({ phone, project_id: projectId });
    setPhone("");
    load();
  }

  async function bulkAdd(e) {
    e.preventDefault();
    if (!bulkPhones || !projectId) return;
    const phones = bulkPhones
      .split(/[\n,;]+/)
      .map((p) => p.trim())
      .filter(Boolean);
    if (phones.length === 0) return;
    await api.bulkAddNumbers({ project_id: projectId, phones });
    setBulkPhones("");
    load();
  }

  async function remove(id) {
    if (!confirm("Remove this number?")) return;
    await api.deleteNumber(id);
    load();
  }

  return (
    <div>
      <h1>Tracking Numbers</h1>

      {/* Add single */}
      <div style={{ display: "flex", gap: 12, marginBottom: 24 }}>
        <form onSubmit={addNumber} style={{ display: "flex", gap: 8, flex: 1 }}>
          <input
            placeholder="+77001234567"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            style={{ maxWidth: 260 }}
          />
          <button type="submit" className="btn">
            Add Number
          </button>
        </form>
      </div>

      {/* Bulk add */}
      <details style={{ marginBottom: 24 }}>
        <summary style={{ cursor: "pointer", color: "var(--text-dim)", fontSize: 14 }}>
          Bulk add numbers
        </summary>
        <form onSubmit={bulkAdd} style={{ marginTop: 8 }}>
          <textarea
            rows={4}
            value={bulkPhones}
            onChange={(e) => setBulkPhones(e.target.value)}
            placeholder="One number per line"
            style={{
              width: "100%",
              maxWidth: 400,
              background: "var(--bg-input)",
              border: "1px solid var(--border)",
              color: "var(--text)",
              padding: 10,
              borderRadius: "var(--radius)",
              fontFamily: "inherit",
              fontSize: 14,
              resize: "vertical",
            }}
          />
          <br />
          <button type="submit" className="btn" style={{ marginTop: 8 }}>
            Bulk Add
          </button>
        </form>
      </details>

      {/* Table */}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Phone</th>
              <th>Type</th>
              <th>Source Label</th>
              <th>Active</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {numbers.length === 0 ? (
              <tr>
                <td colSpan="5" style={{ textAlign: "center", color: "var(--text-dim)" }}>
                  No numbers in pool
                </td>
              </tr>
            ) : (
              numbers.map((n) => (
                <tr key={n.id}>
                  <td>{n.phone}</td>
                  <td>
                    <span className={`badge ${n.number_type === "dynamic" ? "answered" : "busy"}`}>
                      {n.number_type}
                    </span>
                  </td>
                  <td>{n.source_label || "-"}</td>
                  <td>{n.is_active ? "Yes" : "No"}</td>
                  <td>
                    <button className="btn btn-sm btn-danger" onClick={() => remove(n.id)}>
                      Delete
                    </button>
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
