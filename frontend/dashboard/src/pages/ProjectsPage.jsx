import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState([]);
  const [form, setForm] = useState({ name: "", domain: "", default_phone: "" });
  const [showForm, setShowForm] = useState(false);

  function load() {
    api.getProjects().then(setProjects);
  }

  useEffect(load, []);

  async function createProject(e) {
    e.preventDefault();
    await api.createProject(form);
    setForm({ name: "", domain: "", default_phone: "" });
    setShowForm(false);
    load();
  }

  function selectProject(id) {
    localStorage.setItem("kt_project", id);
    window.location.href = "/";
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h1>Projects</h1>
        <button className="btn" onClick={() => setShowForm(!showForm)}>
          {showForm ? "Cancel" : "New Project"}
        </button>
      </div>

      {showForm && (
        <form
          onSubmit={createProject}
          style={{
            background: "var(--bg-card)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            padding: 24,
            marginBottom: 24,
            marginTop: 16,
          }}
        >
          <div className="form-row">
            <div className="form-group">
              <label>Name</label>
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="My Website"
                required
              />
            </div>
            <div className="form-group">
              <label>Domain</label>
              <input
                value={form.domain}
                onChange={(e) => setForm({ ...form, domain: e.target.value })}
                placeholder="example.com"
                required
              />
            </div>
            <div className="form-group">
              <label>Default Phone</label>
              <input
                value={form.default_phone}
                onChange={(e) => setForm({ ...form, default_phone: e.target.value })}
                placeholder="+77001234567"
                required
              />
            </div>
          </div>
          <button type="submit" className="btn">
            Create
          </button>
        </form>
      )}

      <div className="table-wrap" style={{ marginTop: 16 }}>
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Domain</th>
              <th>Default Phone</th>
              <th>API Key</th>
              <th>Active</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {projects.length === 0 ? (
              <tr>
                <td colSpan="6" style={{ textAlign: "center", color: "var(--text-dim)" }}>
                  No projects yet
                </td>
              </tr>
            ) : (
              projects.map((p) => (
                <tr key={p.id}>
                  <td>{p.name}</td>
                  <td>{p.domain}</td>
                  <td>{p.default_phone}</td>
                  <td>
                    <code style={{ fontSize: 11, color: "var(--text-dim)" }}>
                      {p.api_key.slice(0, 12)}...
                    </code>
                  </td>
                  <td>{p.is_active ? "Yes" : "No"}</td>
                  <td>
                    <button className="btn btn-sm" onClick={() => selectProject(p.id)}>
                      Select
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {projects.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <h2 style={{ fontSize: 16, marginBottom: 12 }}>JS Snippet</h2>
          <div
            style={{
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: "var(--radius)",
              padding: 16,
            }}
          >
            <code style={{ fontSize: 12, color: "var(--green)", whiteSpace: "pre-wrap" }}>
              {`<script src="https://YOUR_SERVER/kurotrack.js"\n  data-api="https://YOUR_SERVER/api/v1"\n  data-key="${projects[0]?.api_key || "YOUR_API_KEY"}"\n  data-selector=".kt-phone">\n</script>`}
            </code>
          </div>
        </div>
      )}
    </div>
  );
}
