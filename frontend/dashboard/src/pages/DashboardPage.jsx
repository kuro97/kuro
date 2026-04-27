import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  Legend,
  CartesianGrid,
  ResponsiveContainer,
} from "recharts";
import { api } from "../api";

// --- Утилиты форматирования ---

// Форматирует число: 1200 → "1.2K", 1500000 → "1.5M"
function formatNum(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

// Форматирует деньги: 750000 → "750 000 ₸"
function formatMoney(n) {
  return n.toLocaleString("ru-RU") + " ₸";
}

// Возвращает строку даты в формате YYYY-MM-DD для input type="date"
function toDateInputValue(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function defaultDateFrom() {
  const d = new Date();
  d.setDate(d.getDate() - 7);
  return toDateInputValue(d);
}

function defaultDateTo() {
  return toDateInputValue(new Date());
}

// Конвертирует YYYY-MM-DD в ISO timestamp
function toISOStart(s) { return `${s}T00:00:00`; }
function toISOEnd(s) { return `${s}T23:59:59`; }

// --- Компоненты ---

// KPI-карточка
function KpiCard({ label, value, sub }) {
  return (
    <div
      style={{
        border: "1px solid var(--border, #e5e7eb)",
        borderRadius: 8,
        padding: "16px 20px",
        background: "var(--card-bg, #fff)",
        minWidth: 120,
      }}
    >
      <div style={{ fontSize: 12, color: "var(--text-dim, #6b7280)", marginBottom: 6 }}>
        {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 700, lineHeight: 1 }}>{value}</div>
      {sub && (
        <div style={{ fontSize: 12, color: "var(--text-dim, #6b7280)", marginTop: 4 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

// Кастомный Tooltip для LineChart
function CustomTooltip({ active, payload, label }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div
      style={{
        background: "#fff",
        border: "1px solid #e5e7eb",
        borderRadius: 6,
        padding: "8px 12px",
        fontSize: 13,
      }}
    >
      <div style={{ marginBottom: 4, fontWeight: 600 }}>Дата: {label}</div>
      {payload.map((p) => (
        <div key={p.dataKey} style={{ color: p.color }}>
          {p.name}: {p.value}
        </div>
      ))}
    </div>
  );
}

// Таблица по источникам
function SourceTable({ data }) {
  if (!data || data.length === 0) {
    return <p style={{ color: "var(--text-dim, #6b7280)" }}>Нет данных</p>;
  }
  return (
    <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
      <thead>
        <tr style={{ borderBottom: "1px solid #e5e7eb" }}>
          <th style={{ textAlign: "left", padding: "6px 8px", fontWeight: 600 }}>Источник</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Звонки</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Квалы</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Оплаты</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Выручка</th>
        </tr>
      </thead>
      <tbody>
        {data.map((r) => (
          <tr key={r.source} style={{ borderBottom: "1px solid #f3f4f6" }}>
            <td style={{ padding: "6px 8px" }}>{r.source}</td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>{r.total}</td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>
              {r.qualified} ({r.total ? Math.round((r.qualified / r.total) * 100) : 0}%)
            </td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>
              {r.paid} ({r.total ? Math.round((r.paid / r.total) * 100) : 0}%)
            </td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>
              {formatMoney(r.revenue)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// Таблица по городам
function CityTable({ data }) {
  if (!data || data.length === 0) {
    return <p style={{ color: "var(--text-dim, #6b7280)" }}>Нет данных</p>;
  }
  return (
    <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
      <thead>
        <tr style={{ borderBottom: "1px solid #e5e7eb" }}>
          <th style={{ textAlign: "left", padding: "6px 8px", fontWeight: 600 }}>Город</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Звонки</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Квалы</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Оплаты</th>
          <th style={{ textAlign: "right", padding: "6px 8px", fontWeight: 600 }}>Выручка</th>
        </tr>
      </thead>
      <tbody>
        {data.map((r) => (
          <tr key={r.city} style={{ borderBottom: "1px solid #f3f4f6" }}>
            <td style={{ padding: "6px 8px" }}>{r.city}</td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>{r.total}</td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>
              {r.qualified} ({r.total ? Math.round((r.qualified / r.total) * 100) : 0}%)
            </td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>
              {r.paid} ({r.total ? Math.round((r.paid / r.total) * 100) : 0}%)
            </td>
            <td style={{ textAlign: "right", padding: "6px 8px" }}>
              {formatMoney(r.revenue)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// --- Главная страница дашборда ---
export default function DashboardPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(localStorage.getItem("kt_project") || "");

  // Фильтры дат: читаем из URL, дефолт — последние 7 дней
  const [dateFrom, setDateFrom] = useState(
    searchParams.get("date_from") || defaultDateFrom()
  );
  const [dateTo, setDateTo] = useState(
    searchParams.get("date_to") || defaultDateTo()
  );

  // Загружаем проекты при монтировании
  useEffect(() => {
    api.getProjects().then((p) => {
      setProjects(p);
      if (!projectId && p.length > 0) {
        const firstId = p[0].id;
        setProjectId(firstId);
        localStorage.setItem("kt_project", firstId);
      }
    });
  }, []);

  // Загружаем статистику при изменении проекта или дат
  function fetchStats(pid, from, to) {
    if (!pid) return;
    setLoading(true);
    api
      .getDashboardStats(pid, toISOStart(from), toISOEnd(to))
      .then((data) => {
        setStats(data);
        // Синхронизируем URL с фильтрами
        setSearchParams({ date_from: from, date_to: to }, { replace: true });
      })
      .catch(() => setStats(null))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (projectId) fetchStats(projectId, dateFrom, dateTo);
  }, [projectId]);

  function handleRefresh() {
    fetchStats(projectId, dateFrom, dateTo);
  }

  function selectProject(id) {
    setProjectId(id);
    localStorage.setItem("kt_project", id);
  }

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <h1 style={{ margin: 0 }}>Дашборд</h1>
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

      {/* Фильтр по датам */}
      <div className="form-row" style={{ marginBottom: 20 }}>
        <div className="form-group">
          <label>Дата от</label>
          <input
            type="date"
            value={dateFrom}
            max={dateTo}
            onChange={(e) => setDateFrom(e.target.value)}
          />
        </div>
        <div className="form-group">
          <label>Дата до</label>
          <input
            type="date"
            value={dateTo}
            min={dateFrom}
            onChange={(e) => setDateTo(e.target.value)}
          />
        </div>
        <div className="form-group" style={{ alignSelf: "flex-end" }}>
          <button
            onClick={handleRefresh}
            disabled={loading || !projectId}
            style={{
              padding: "6px 20px",
              cursor: loading || !projectId ? "not-allowed" : "pointer",
              opacity: loading || !projectId ? 0.7 : 1,
            }}
          >
            {loading ? "Загрузка..." : "Обновить"}
          </button>
        </div>
      </div>

      {!projectId && projects.length === 0 && (
        <p style={{ color: "var(--text-dim, #6b7280)" }}>
          Нет проектов. Создайте проект в разделе Projects.
        </p>
      )}

      {stats && (
        <>
          {/* KPI карточки */}
          <div
            style={{
              display: "flex",
              gap: 12,
              flexWrap: "wrap",
              marginBottom: 24,
            }}
          >
            <KpiCard label="Звонков" value={stats.total} />
            <KpiCard
              label="Отвечено"
              value={stats.answered}
              sub={stats.total ? `${Math.round((stats.answered / stats.total) * 100)}%` : "0%"}
            />
            <KpiCard
              label="Квалов"
              value={`${stats.qualified} (${stats.qualified_pct}%)`}
            />
            <KpiCard
              label="Оплат"
              value={`${stats.paid} (${stats.paid_pct}%)`}
            />
            <KpiCard
              label="Выручка"
              value={formatMoney(stats.revenue)}
            />
          </div>

          {/* Линейный график звонков по дням */}
          <div
            style={{
              border: "1px solid var(--border, #e5e7eb)",
              borderRadius: 8,
              padding: "16px 20px",
              background: "var(--card-bg, #fff)",
              marginBottom: 24,
            }}
          >
            <div style={{ fontWeight: 600, marginBottom: 16 }}>Звонки по дням</div>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={stats.by_day} margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey="day"
                  tick={{ fontSize: 11 }}
                  tickFormatter={(v) => v.slice(5)} // показываем MM-DD
                />
                <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                <Tooltip content={<CustomTooltip />} />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="total"
                  name="Звонки"
                  stroke="#6366f1"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                />
                <Line
                  type="monotone"
                  dataKey="qualified"
                  name="Квалы"
                  stroke="#22c55e"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                />
                <Line
                  type="monotone"
                  dataKey="paid"
                  name="Оплаты"
                  stroke="#eab308"
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 4 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Таблицы по источникам и городам */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: 16,
            }}
          >
            <div
              style={{
                border: "1px solid var(--border, #e5e7eb)",
                borderRadius: 8,
                padding: "16px 20px",
                background: "var(--card-bg, #fff)",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 12 }}>По источникам</div>
              <SourceTable data={stats.by_source} />
            </div>
            <div
              style={{
                border: "1px solid var(--border, #e5e7eb)",
                borderRadius: 8,
                padding: "16px 20px",
                background: "var(--card-bg, #fff)",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 12 }}>По городам</div>
              <CityTable data={stats.by_city} />
            </div>
          </div>
        </>
      )}

      {!stats && projectId && !loading && (
        <p style={{ color: "var(--text-dim, #6b7280)" }}>
          Нажмите «Обновить» для загрузки данных.
        </p>
      )}
    </div>
  );
}
