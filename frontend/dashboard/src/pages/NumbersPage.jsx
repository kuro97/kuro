import React, { useEffect, useState } from "react";
import { PlusCircle, Trash2 } from "lucide-react";
import { api } from "../api";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

export default function NumbersPage() {
  const [numbers, setNumbers] = useState([]);
  const [phone, setPhone] = useState("");
  const [bulkPhones, setBulkPhones] = useState("");
  const [showBulk, setShowBulk] = useState(false);
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
    setShowBulk(false);
    load();
  }

  async function remove(id) {
    if (!confirm("Удалить этот номер?")) return;
    await api.deleteNumber(id);
    load();
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Трекинговые номера</h1>

      {/* Добавить один номер */}
      <form onSubmit={addNumber} className="flex items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <Label>Номер телефона</Label>
          <Input
            placeholder="+77001234567"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            className="w-60"
          />
        </div>
        <Button type="submit" size="sm" className="gap-2">
          <PlusCircle size={14} />
          Добавить
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setShowBulk(!showBulk)}
        >
          {showBulk ? "Скрыть" : "Массовое добавление"}
        </Button>
      </form>

      {/* Массовое добавление */}
      {showBulk && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Массовое добавление</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={bulkAdd} className="space-y-3">
              <textarea
                rows={4}
                value={bulkPhones}
                onChange={(e) => setBulkPhones(e.target.value)}
                placeholder="Один номер на строку"
                className="w-full max-w-md rounded-md border border-input bg-transparent px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring resize-vertical"
              />
              <Button type="submit" size="sm">Добавить всё</Button>
            </form>
          </CardContent>
        </Card>
      )}

      {/* Таблица номеров */}
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Телефон</TableHead>
                <TableHead>Тип</TableHead>
                <TableHead>Метка источника</TableHead>
                <TableHead>Активен</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {numbers.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5} className="text-center text-muted-foreground py-10">
                    Нет номеров в пуле
                  </TableCell>
                </TableRow>
              ) : (
                numbers.map((n) => (
                  <TableRow key={n.id}>
                    <TableCell className="font-mono">{n.phone}</TableCell>
                    <TableCell>
                      <Badge variant={n.number_type === "dynamic" ? "dynamic" : "static"}>
                        {n.number_type}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground">{n.source_label || "—"}</TableCell>
                    <TableCell>
                      <Badge variant={n.is_active ? "answered" : "noAnswer"}>
                        {n.is_active ? "Да" : "Нет"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="text-destructive hover:text-destructive hover:bg-destructive/10"
                        onClick={() => remove(n.id)}
                      >
                        <Trash2 size={15} />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
