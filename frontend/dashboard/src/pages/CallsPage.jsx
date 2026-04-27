import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { SourceIcon } from "../components/SourceIcon";

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

// Обрезает длинное UTM-значение до 20 символов с многоточием
const UTM_MAX = 20;
function truncateUtm(val) {
  if (!val) return "-";
  if (val.length > UTM_MAX) return val.slice(0, UTM_MAX) + "…";
  return val;
}

// Возвращает строку даты в формате YYYY-MM-DD для input type="date"
function toDateInputValue(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

// Последние 7 дней: date_to = сегодня, date_from = сегодня - 7 дней
function defaultDateFrom() {
  const d = new Date();
  d.setDate(d.getDate() - 7);
  return toDateInputValue(d);
}

function defaultDateTo() {
  return toDateInputValue(new Date());
}

// Конвертирует YYYY-MM-DD в ISO timestamp начала дня (00:00:00)
function toISOStart(dateStr) {
  return `${dateStr}T00:00:00`;
}

// Конвертирует YYYY-MM-DD в ISO timestamp конца дня (23:59:59)
function toISOEnd(dateStr) {
  return `${dateStr}T23:59:59`;
}

export default function CallsPage() {
  const [calls, setCalls] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const projectId = localStorage.getItem("kt_project");

  // Читаем фильтры из URL, если их нет — ставим дефолтные значения
  const [filter, setFilter] = useState({
    source: searchParams.get("source") || "",
    disposition: searchParams.get("disposition") || "",
    date_from: searchParams.get("date_from") || defaultDateFrom(),
    date_to: searchParams.get("date_to") || defaultDateTo(),
  });

  // Выполняет запрос звонков с текущим фильтром
  function fetchCalls(currentFilter) {
    if (!projectId) return;
    setLoading(true);

    const apiParams = {};
    if (currentFilter.source) apiParams.source = currentFilter.source;
    if (currentFilter.disposition) apiParams.disposition = currentFilter.disposition;
    if (currentFilter.date_from) apiParams.date_from = toISOStart(currentFilter.date_from);
    if (currentFilter.date_to) apiParams.date_to = toISOEnd(currentFilter.date_to);

    api.getCalls(projectId, apiParams)
      .then(setCalls)
      .catch(() => setCalls([]))
      .finally(() => setLoading(false));
  }

  // Обработчик кнопки «Обновить» — перезапрашивает данные с теми же параметрами
  function handleRefresh() {
    fetchCalls(filter);
  }

  // При изменении фильтра — обновляем URL и перезапрашиваем данные
  useEffect(() => {
    // Синхронизируем query string с текущим состоянием фильтра
    const params = {};
    if (filter.source) params.source = filter.source;
    if (filter.disposition) params.disposition = filter.disposition;
    if (filter.date_from) params.date_from = filter.date_from;
    if (filter.date_to) params.date_to = filter.date_to;
    setSearchParams(params, { replace: true });

    fetchCalls(filter);
  }, [projectId, filter]);

  return (
    <div>
      <h1>Calls</h1>

      <div className="form-row" style={{ marginBottom: 16 }}>
        {/* Фильтр по дате: От */}
        <div className="form-group">
          <label>От</label>
          <input
            type="date"
            value={filter.date_from}
            max={filter.date_to}
            onChange={(e) => setFilter({ ...filter, date_from: e.target.value })}
          />
        </div>
        {/* Фильтр по дате: До */}
        <div className="form-group">
          <label>До</label>
          <input
            type="date"
            value={filter.date_to}
            min={filter.date_from}
            onChange={(e) => setFilter({ ...filter, date_to: e.target.value })}
          />
        </div>
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
        {/* Кнопка ручного обновления списка звонков */}
        <div className="form-group" style={{ alignSelf: "flex-end" }}>
          <button
            className="btn"
            onClick={handleRefresh}
            disabled={loading}
            style={{ cursor: loading ? "not-allowed" : "pointer", opacity: loading ? 0.7 : 1 }}
          >
            {loading ? "Загрузка..." : "Обновить"}
          </button>
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
              <th>Medium</th>
              <th>Campaign</th>
              <th>Keyword</th>
              <th>City</th>
              <th>Duration</th>
              <th>Status</th>
              <th>Recording</th>
            </tr>
          </thead>
          <tbody>
            {calls.length === 0 ? (
              <tr>
                <td colSpan="11" style={{ textAlign: "center", color: "var(--text-dim)" }}>
                  Нет звонков за выбранный период
                </td>
              </tr>
            ) : (
              calls.map((c) => (
                <tr key={c.id}>
                  <td>{formatDate(c.started_at)}</td>
                  <td>{c.caller_number}</td>
                  <td>{c.tracking_did}</td>
                  <td><SourceIcon source={c.source || "direct"} /></td>
                  <td title={c.medium || undefined}>{truncateUtm(c.medium)}</td>
                  <td title={c.campaign || undefined}>{truncateUtm(c.campaign)}</td>
                  <td title={c.keyword || undefined}>{truncateUtm(c.keyword)}</td>
                  <td>{c.amo_city || "-"}</td>
                  <td>{formatDuration(c.billsec)}</td>
                  <td>
                    {(() => {
                      // Маппинг disposition → CSS-класс и метка
                      const statusClass = {
                        "ANSWERED":  "status-badge status-answered",
                        "NO ANSWER": "status-badge status-no-answer",
                        "FAILED":    "status-badge status-failed",
                        "BUSY":      "status-badge status-busy",
                      }[c.disposition] || "status-badge status-no-answer";
                      return <span className={statusClass}>{c.disposition || "—"}</span>;
                    })()}
                  </td>
                  <td>
                    {c.recording_url ? (
                      <audio controls preload="none" style={{ height: 28, maxWidth: 180 }}>
                        <source src={c.recording_url} />
                      </audio>
                    ) : (
                      <span style={{ color: "var(--text-dim)", fontSize: 12 }}>-</span>
                    )}
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
