import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { RefreshCw } from "lucide-react";
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
import { SourceIcon } from "../components/SourceIcon";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "../components/ui/card";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Select, SelectItem } from "../components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

// --- Утилиты форматирования ---

// Форматирует число: 1200 → "1.2K"
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

function toISOStart(s) { return `${s}T00:00:00`; }
function toISOEnd(s) { return `${s}T23:59:59`; }

// --- KPI карточка ---
function KpiCard({ label, value, sub }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-3xl font-bold">{value}</CardTitle>
      </CardHeader>
      {sub && (
        <CardContent>
          <p className="text-xs text-muted-foreground">{sub}</p>
        </CardContent>
      )}
    </Card>
  );
}

// Кастомный Tooltip для recharts — стилизован под тёмную тему
function CustomTooltip({ active, payload, label }) {
  if (!active || !payload || !payload.length) return null;
  return (
    <div className="rounded-lg border border-border bg-popover px-3 py-2 text-sm shadow-md">
      <div className="mb-1 font-semibold text-foreground">{label}</div>
      {payload.map((p) => (
        <div key={p.dataKey} style={{ color: p.color }}>
          {p.name}: {p.value}
        </div>
      ))}
    </div>
  );
}

// --- Таблица по источникам ---
function SourceTable({ data }) {
  if (!data || data.length === 0) {
    return <p className="text-sm text-muted-foreground">Нет данных</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Источник</TableHead>
          <TableHead className="text-right">Звонки</TableHead>
          <TableHead className="text-right">Квалы</TableHead>
          <TableHead className="text-right">Оплаты</TableHead>
          <TableHead className="text-right">Выручка</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((r) => (
          <TableRow key={r.source}>
            <TableCell><SourceIcon source={r.source || "direct"} /></TableCell>
            <TableCell className="text-right">{r.total}</TableCell>
            <TableCell className="text-right">
              {r.qualified} ({r.total ? Math.round((r.qualified / r.total) * 100) : 0}%)
            </TableCell>
            <TableCell className="text-right">
              {r.paid} ({r.total ? Math.round((r.paid / r.total) * 100) : 0}%)
            </TableCell>
            <TableCell className="text-right">{formatMoney(r.revenue)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// --- Таблица по городам ---
function CityTable({ data }) {
  if (!data || data.length === 0) {
    return <p className="text-sm text-muted-foreground">Нет данных</p>;
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Город</TableHead>
          <TableHead className="text-right">Звонки</TableHead>
          <TableHead className="text-right">Квалы</TableHead>
          <TableHead className="text-right">Оплаты</TableHead>
          <TableHead className="text-right">Выручка</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {data.map((r) => (
          <TableRow key={r.city}>
            <TableCell>{r.city}</TableCell>
            <TableCell className="text-right">{r.total}</TableCell>
            <TableCell className="text-right">
              {r.qualified} ({r.total ? Math.round((r.qualified / r.total) * 100) : 0}%)
            </TableCell>
            <TableCell className="text-right">
              {r.paid} ({r.total ? Math.round((r.paid / r.total) * 100) : 0}%)
            </TableCell>
            <TableCell className="text-right">{formatMoney(r.revenue)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// --- Главная страница ---
export default function DashboardPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [projects, setProjects] = useState([]);
  const [projectId, setProjectId] = useState(localStorage.getItem("kt_project") || "");

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

  function fetchStats(pid, from, to) {
    if (!pid) return;
    setLoading(true);
    api
      .getDashboardStats(pid, toISOStart(from), toISOEnd(to))
      .then((data) => {
        setStats(data);
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
    <div className="space-y-6">
      {/* Заголовок */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Дашборд</h1>
        {projects.length > 1 && (
          <Select
            value={projectId}
            onChange={(e) => selectProject(e.target.value)}
            className="w-48"
          >
            {projects.map((p) => (
              <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
            ))}
          </Select>
        )}
      </div>

      {/* Фильтр по датам */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <Label>Дата от</Label>
          <Input
            type="date"
            value={dateFrom}
            max={dateTo}
            onChange={(e) => setDateFrom(e.target.value)}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Дата до</Label>
          <Input
            type="date"
            value={dateTo}
            min={dateFrom}
            onChange={(e) => setDateTo(e.target.value)}
            className="w-40"
          />
        </div>
        <Button
          onClick={handleRefresh}
          disabled={loading || !projectId}
          size="sm"
          className="gap-2"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          {loading ? "Загрузка..." : "Обновить"}
        </Button>
      </div>

      {!projectId && projects.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Нет проектов. Создайте проект в разделе «Проекты».
        </p>
      )}

      {stats && (
        <>
          {/* KPI карточки — 2 колонки на mobile, 5 на desktop */}
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            {/* Карточка "Звонки" показывает уникальные + попытки дозвона */}
            <KpiCard
              label="Звонков"
              value={formatNum(stats.total)}
              sub={stats.total_attempts != null ? `${stats.total_attempts} попыток дозвона` : undefined}
            />
            <KpiCard
              label="Отвечено"
              value={formatNum(stats.answered)}
              sub={stats.total ? `${Math.round((stats.answered / stats.total) * 100)}% от общего` : "0%"}
            />
            <KpiCard
              label="Квалов"
              value={formatNum(stats.qualified)}
              sub={`${stats.qualified_pct}%`}
            />
            <KpiCard
              label="Оплат"
              value={formatNum(stats.paid)}
              sub={`${stats.paid_pct}%`}
            />
            <KpiCard
              label="Выручка"
              value={formatMoney(stats.revenue)}
            />
          </div>

          {/* График звонков по дням */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Звонки по дням</CardTitle>
            </CardHeader>
            <CardContent>
              <ResponsiveContainer width="100%" height={240}>
                <LineChart data={stats.by_day} margin={{ top: 4, right: 16, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(217.2 32.6% 20%)" />
                  <XAxis
                    dataKey="day"
                    tick={{ fontSize: 11, fill: "hsl(215 20.2% 65.1%)" }}
                    axisLine={{ stroke: "hsl(217.2 32.6% 20%)" }}
                    tickLine={{ stroke: "hsl(217.2 32.6% 20%)" }}
                    tickFormatter={(v) => v.slice(5)}
                  />
                  <YAxis
                    tick={{ fontSize: 11, fill: "hsl(215 20.2% 65.1%)" }}
                    axisLine={{ stroke: "hsl(217.2 32.6% 20%)" }}
                    tickLine={{ stroke: "hsl(217.2 32.6% 20%)" }}
                    allowDecimals={false}
                  />
                  <Tooltip content={<CustomTooltip />} />
                  <Legend wrapperStyle={{ color: "hsl(215 20.2% 65.1%)", fontSize: 12 }} />
                  <Line
                    type="monotone"
                    dataKey="total"
                    name="Звонки"
                    stroke="#3b82f6"
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 4 }}
                  />
                  <Line
                    type="monotone"
                    dataKey="qualified"
                    name="Квалы"
                    stroke="#10b981"
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 4 }}
                  />
                  <Line
                    type="monotone"
                    dataKey="paid"
                    name="Оплаты"
                    stroke="#f59e0b"
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 4 }}
                  />
                </LineChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          {/* Таблицы по источникам и городам */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">По источникам</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <SourceTable data={stats.by_source} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="text-base">По городам</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <CityTable data={stats.by_city} />
              </CardContent>
            </Card>
          </div>
        </>
      )}

      {!stats && projectId && !loading && (
        <p className="text-sm text-muted-foreground">
          Нажмите «Обновить» для загрузки данных.
        </p>
      )}
    </div>
  );
}
