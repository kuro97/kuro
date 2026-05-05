import React, { useEffect, useState, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { RefreshCw, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";
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

// Размер страницы — дефолт 100, максимум 200
const PAGE_SIZE = 100;

export default function CallsPage() {
  const [calls, setCalls] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  // Раскрытая строка для записи (опциональная деталь)
  const [expandedId, setExpandedId] = useState(null);
  const projectId = localStorage.getItem("kt_project");

  // Текущая страница (1-based), берём из URL
  const [page, setPage] = useState(() => {
    const p = parseInt(searchParams.get("page") || "1", 10);
    return p > 0 ? p : 1;
  });

  // dedupe=true — уникальные звонки (по linkedid); false — все legs
  const [dedupe, setDedupe] = useState(searchParams.get("dedupe") !== "false");

  const [filter, setFilter] = useState({
    source: searchParams.get("source") || "",
    disposition: searchParams.get("disposition") || "",
    date_from: searchParams.get("date_from") || defaultDateFrom(),
    date_to: searchParams.get("date_to") || defaultDateTo(),
  });

  // Рассчитываем общее количество страниц
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const fetchCalls = useCallback((currentFilter, currentDedupe, currentPage) => {
    if (!projectId) return;
    setLoading(true);

    const apiParams = {
      limit: PAGE_SIZE,
      offset: (currentPage - 1) * PAGE_SIZE,
    };
    if (currentFilter.source) apiParams.source = currentFilter.source;
    if (currentFilter.disposition) apiParams.disposition = currentFilter.disposition;
    if (currentFilter.date_from) apiParams.date_from = toISOStart(currentFilter.date_from);
    if (currentFilter.date_to) apiParams.date_to = toISOEnd(currentFilter.date_to);
    // Передаём dedupe только если false — бэкенд по умолчанию true
    if (!currentDedupe) apiParams.dedupe = "false";

    api.getCalls(projectId, apiParams)
      .then((data) => {
        // Бэкенд возвращает {items, total}
        setCalls(data.items || []);
        setTotal(data.total || 0);
      })
      .catch(() => {
        setCalls([]);
        setTotal(0);
      })
      .finally(() => setLoading(false));
  }, [projectId]);

  function handleRefresh() {
    fetchCalls(filter, dedupe, page);
  }

  // Обновляем URL и делаем fetch при изменении фильтров, dedupe или page
  useEffect(() => {
    const params = {};
    if (filter.source) params.source = filter.source;
    if (filter.disposition) params.disposition = filter.disposition;
    if (filter.date_from) params.date_from = filter.date_from;
    if (filter.date_to) params.date_to = filter.date_to;
    // Сохраняем dedupe в URL только если false (default — true, не засоряем URL)
    if (!dedupe) params.dedupe = "false";
    // Сохраняем page только если >1 (default — 1, не засоряем URL)
    if (page > 1) params.page = String(page);
    setSearchParams(params, { replace: true });

    fetchCalls(filter, dedupe, page);
  }, [projectId, filter, dedupe, page]);

  // При смене фильтров или dedupe — сбрасываем на первую страницу
  function handleFilterChange(newFilter) {
    setFilter(newFilter);
    setPage(1);
  }

  function handleDedupeChange(newDedupe) {
    setDedupe(newDedupe);
    setPage(1);
  }

  // Диапазон записей на текущей странице (для текста "Показано X-Y из Z")
  const rangeFrom = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const rangeTo = Math.min(page * PAGE_SIZE, total);

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
            onChange={(e) => handleFilterChange({ ...filter, date_from: e.target.value })}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>До</Label>
          <Input
            type="date"
            value={filter.date_to}
            min={filter.date_from}
            onChange={(e) => handleFilterChange({ ...filter, date_to: e.target.value })}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Источник</Label>
          <Input
            placeholder="google, instagram..."
            value={filter.source}
            onChange={(e) => handleFilterChange({ ...filter, source: e.target.value })}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Статус</Label>
          <Select
            value={filter.disposition}
            onChange={(e) => handleFilterChange({ ...filter, disposition: e.target.value })}
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
            onChange={(e) => handleDedupeChange(e.target.checked)}
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
                      <TableCell>
                        <div className="flex items-center gap-1.5">
                          <SourceIcon source={c.source || "direct"} />
                          {/* Показываем badge если у звонка есть хотя бы один UTM-параметр */}
                          {(c.medium || c.campaign || c.keyword) && (
                            <span className="inline-flex items-center rounded px-1 py-0.5 text-[10px] font-medium bg-blue-500/15 text-blue-500 leading-none">
                              UTM
                            </span>
                          )}
                        </div>
                      </TableCell>
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

          {/* Пагинация */}
          <div className="flex items-center justify-between px-4 py-3 border-t">
            <span className="text-sm text-muted-foreground">
              {total === 0
                ? "Нет звонков"
                : `Показано ${rangeFrom}–${rangeTo} из ${total}`}
            </span>
            <div className="flex items-center gap-1">
              {/* Первая страница */}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage(1)}
                disabled={page <= 1 || loading}
                title="Первая страница"
              >
                <ChevronsLeft size={14} />
              </Button>
              {/* Назад */}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={page <= 1 || loading}
              >
                <ChevronLeft size={14} />
                Назад
              </Button>
              {/* Номер страницы */}
              <span className="px-3 text-sm text-muted-foreground">
                Стр. {page} / {totalPages}
              </span>
              {/* Вперёд */}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages || loading}
              >
                Вперёд
                <ChevronRight size={14} />
              </Button>
              {/* Последняя страница */}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage(totalPages)}
                disabled={page >= totalPages || loading}
                title="Последняя страница"
              >
                <ChevronsRight size={14} />
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
