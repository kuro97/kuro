const API_BASE = "/api/v1";

function getToken() {
  return localStorage.getItem("kt_token");
}

async function request(path, options = {}) {
  const token = getToken();
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (res.status === 401) {
    localStorage.removeItem("kt_token");
    window.location.href = "/login";
    return;
  }

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }

  return res.json();
}

export const api = {
  // Auth
  login: (email, password) =>
    request("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `username=${encodeURIComponent(email)}&password=${encodeURIComponent(password)}`,
    }),
  me: () => request("/auth/me"),

  // Projects
  getProjects: () => request("/projects/"),
  createProject: (data) =>
    request("/projects/", { method: "POST", body: JSON.stringify(data) }),

  // Calls
  // dedupe: true (по умолчанию) — уникальные звонки; false — все legs
  getCalls: (projectId, params = {}) => {
    const qs = new URLSearchParams({ project_id: projectId, ...params }).toString();
    return request(`/calls/?${qs}`);
  },
  getCallStats: (projectId, days = 30) =>
    request(`/calls/stats?project_id=${projectId}&days=${days}`),
  getDailyChart: (projectId, days = 30) =>
    request(`/calls/chart/daily?project_id=${projectId}&days=${days}`),
  getSourcesChart: (projectId, days = 30) =>
    request(`/calls/chart/sources?project_id=${projectId}&days=${days}`),

  // Новый stats endpoint с date_from/date_to и группировками
  getDashboardStats: (projectId, dateFrom, dateTo) => {
    const params = new URLSearchParams({ project_id: projectId });
    if (dateFrom) params.append("date_from", dateFrom);
    if (dateTo) params.append("date_to", dateTo);
    return request(`/calls/stats?${params.toString()}`);
  },

  // Numbers
  getNumbers: (projectId) => request(`/numbers/?project_id=${projectId}`),
  addNumber: (data) => request("/numbers/", { method: "POST", body: JSON.stringify(data) }),
  bulkAddNumbers: (data) =>
    request("/numbers/bulk", { method: "POST", body: JSON.stringify(data) }),
  deleteNumber: (id) => request(`/numbers/${id}`, { method: "DELETE" }),

  // Pool stats
  getPoolStats: (apiKey) =>
    fetch(`${API_BASE}/tracking/pool-stats`, {
      headers: { "X-Api-Key": apiKey },
    }).then((r) => r.json()),
};
