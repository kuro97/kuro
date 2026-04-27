import React, { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { RefreshCw, Play } from "lucide-react";
import { api } from "../api";
import { SourceIcon } from "../components/SourceIcon";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Select, SelectItem } from "../components/ui/select";
import { Badge } from "../components/ui/badge";
import { Card, CardContent } from "../components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

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

const UTM_MAX = 20;
function truncateUtm(val) {
  if (!val) return "-";
  if (val.length > UTM_MAX) return val.slice(0, UTM_MAX) + "…";
  return val;
}

function toDateInputValue(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function defaultDateFrom() {
  const d = new Date();
  d.setDate(d.getDate() - 7);
  return toDateInputValue(d);
}

function defaultDateTo() {
  return toDateInputValue(new Date());
}

function toISOStart(dateStr) { return `${dateStr}T00:00:00`; }
function toISOEnd(dateStr) { return `${dateStr}T23:59:59`; }

// Маппинг disposition → вариант Badge
const dispositionVariant = {
  "ANSWERED": "answered",
  "NO ANSWER": "noAnswer",
  "FAILED": "failed",
  "BUSY": "busy",
};

export default function CallsPage() {
  const [calls, setCalls] = useState([]);
  const [loading, setLoading] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  // Раскрытая строка для записи (опциональная деталь)
  const [expandedId, setExpandedId] = useState(null);
  const projectId = localStorage.getItem("kt_project");

  // dedupe=true — уникальные звонки (по linkedid); false — все legs
  const [dedupe, setDedupe] = useState(searchParams.get("dedupe") !== "false");

  const [filter, setFilter] = useState({
    source: searchParams.get("source") || "",
    disposition: searchParams.get("disposition") || "",
    date_from: searchParams.get("date_from") || defaultDateFrom(),
    date_to: searchParams.get("date_to") || defaultDateTo(),
  });

  function fetchCalls(currentFilter, currentDedupe) {
    if (!projectId) return;
    setLoading(true);

    const apiParams = {};
    if (currentFilter.source) apiParams.source = currentFilter.source;
    if (currentFilter.disposition) apiParams.disposition = currentFilter.disposition;
    if (currentFilter.date_from) apiParams.date_from = toISOStart(currentFilter.date_from);
    if (currentFilter.date_to) apiParams.date_to = toISOEnd(currentFilter.date_to);
    // Передаём dedupe только если false — бэкенд по умолчанию true
    if (!currentDedupe) apiParams.dedupe = "false";

    api.getCalls(projectId, apiParams)
      .then(setCalls)
      .catch(() => setCalls([]))
      .finally(() => setLoading(false));
  }

  function handleRefresh() {
    fetchCalls(filter, dedupe);
  }

  useEffect(() => {
    const params = {};
    if (filter.source) params.source = filter.source;
    if (filter.disposition) params.disposition = filter.disposition;
    if (filter.date_from) params.date_from = filter.date_from;
    if (filter.date_to) params.date_to = filter.date_to;
    // Сохраняем dedupe в URL только если false (default — true, не засоряем URL)
    if (!dedupe) params.dedupe = "false";
    setSearchParams(params, { replace: true });

    fetchCalls(filter, dedupe);
  }, [projectId, filter, dedupe]);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Звонки</h1>

      {/* Фильтры */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <Label>От</Label>
          <Input
            type="date"
            value={filter.date_from}
            max={filter.date_to}
            onChange={(e) => setFilter({ ...filter, date_from: e.target.value })}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>До</Label>
          <Input
            type="date"
            value={filter.date_to}
            min={filter.date_from}
            onChange={(e) => setFilter({ ...filter, date_to: e.target.value })}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Источник</Label>
          <Input
            placeholder="google, instagram..."
            value={filter.source}
            onChange={(e) => setFilter({ ...filter, source: e.target.value })}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Статус</Label>
          <Select
            value={filter.disposition}
            onChange={(e) => setFilter({ ...filter, disposition: e.target.value })}
            className="w-36"
          >
            <SelectItem value="">Все</SelectItem>
            <SelectItem value="ANSWERED">Отвечен</SelectItem>
            <SelectItem value="NO ANSWER">Пропущен</SelectItem>
            <SelectItem value="BUSY">Занято</SelectItem>
            <SelectItem value="FAILED">Ошибка</SelectItem>
          </Select>
        </div>
        <Button
          onClick={handleRefresh}
          disabled={loading}
          size="sm"
          className="gap-2"
        >
          <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          {loading ? "Загрузка..." : "Обновить"}
        </Button>
        {/* Переключатель дедупликации — скрывает/показывает дубли legs */}
        <label className="flex items-center gap-2 cursor-pointer select-none text-sm">
          <input
            type="checkbox"
            checked={dedupe}
            onChange={(e) => setDedupe(e.target.checked)}
            className="h-4 w-4 rounded border-border accent-primary"
          />
          <span>Только уникальные звонки</span>
        </label>
      </div>

      {/* Таблица звонков */}
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Дата</TableHead>
                <TableHead>Звонящий</TableHead>
                <TableHead>Номер</TableHead>
                <TableHead>Источник</TableHead>
                <TableHead>Кампания</TableHead>
                <TableHead>Город</TableHead>
                <TableHead>Длит.</TableHead>
                <TableHead>Статус</TableHead>
                <TableHead>Запись</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {calls.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={9}
                    className="text-center text-muted-foreground py-10"
                  >
                    {loading ? "Загрузка..." : "Нет звонков за выбранный период"}
                  </TableCell>
                </TableRow>
              ) : (
                calls.map((c) => (
                  <React.Fragment key={c.id}>
                    <TableRow
                      className="cursor-pointer"
                      onClick={() => setExpandedId(expandedId === c.id ? null : c.id)}
                    >
                      <TableCell className="text-sm">{formatDate(c.started_at)}</TableCell>
                      <TableCell className="font-mono text-xs">{c.caller_number}</TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">{c.tracking_did}</TableCell>
                      <TableCell><SourceIcon source={c.source || "direct"} /></TableCell>
                      <TableCell
                        className="text-xs text-muted-foreground max-w-32 truncate"
                        title={c.campaign || undefined}
                      >
                        {truncateUtm(c.campaign)}
                      </TableCell>
                      <TableCell className="text-sm">{c.amo_city || "-"}</TableCell>
                      <TableCell className="text-sm tabular-nums">{formatDuration(c.billsec)}</TableCell>
                      <TableCell>
                        <Badge variant={dispositionVariant[c.disposition] || "noAnswer"}>
                          {c.disposition || "—"}
                        </Badge>
                      </TableCell>
                      <TableCell onClick={(e) => e.stopPropagation()}>
                        {c.recording_url ? (
                          <audio controls preload="none" className="h-7 max-w-48">
                            <source src={c.recording_url} />
                          </audio>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    </TableRow>
                    {/* Раскрытая строка с деталями */}
                    {expandedId === c.id && (
                      <TableRow className="bg-muted/30">
                        <TableCell colSpan={9} className="py-3 px-6">
                          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs text-muted-foreground">
                            <div>
                              <div className="font-medium text-foreground mb-0.5">Medium</div>
                              <div title={c.medium || undefined}>{c.medium || "—"}</div>
                            </div>
                            <div>
                              <div className="font-medium text-foreground mb-0.5">Keyword</div>
                              <div title={c.keyword || undefined}>{c.keyword || "—"}</div>
                            </div>
                            <div>
                              <div className="font-medium text-foreground mb-0.5">Кампания (полная)</div>
                              <div>{c.campaign || "—"}</div>
                            </div>
                            <div>
                              <div className="font-medium text-foreground mb-0.5">Tracking DID</div>
                              <div className="font-mono">{c.tracking_did || "—"}</div>
                            </div>
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                  </React.Fragment>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
