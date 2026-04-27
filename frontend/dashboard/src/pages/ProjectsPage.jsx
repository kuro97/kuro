import React, { useEffect, useState } from "react";
import { PlusCircle, CheckCircle } from "lucide-react";
import { api } from "../api";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Separator } from "../components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";

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
    <div className="space-y-6">
      {/* Заголовок */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Проекты</h1>
        <Button
          size="sm"
          variant={showForm ? "outline" : "default"}
          className="gap-2"
          onClick={() => setShowForm(!showForm)}
        >
          <PlusCircle size={14} />
          {showForm ? "Отмена" : "Новый проект"}
        </Button>
      </div>

      {/* Форма создания проекта */}
      {showForm && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium">Создать проект</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={createProject} className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div className="flex flex-col gap-1.5">
                  <Label>Название</Label>
                  <Input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="Мой сайт"
                    required
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label>Домен</Label>
                  <Input
                    value={form.domain}
                    onChange={(e) => setForm({ ...form, domain: e.target.value })}
                    placeholder="example.com"
                    required
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label>Основной телефон</Label>
                  <Input
                    value={form.default_phone}
                    onChange={(e) => setForm({ ...form, default_phone: e.target.value })}
                    placeholder="+77001234567"
                    required
                  />
                </div>
              </div>
              <Button type="submit" size="sm">Создать</Button>
            </form>
          </CardContent>
        </Card>
      )}

      {/* Таблица проектов */}
      <Card>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Название</TableHead>
                <TableHead>Домен</TableHead>
                <TableHead>Телефон</TableHead>
                <TableHead>API Key</TableHead>
                <TableHead>Активен</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {projects.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground py-10">
                    Нет проектов
                  </TableCell>
                </TableRow>
              ) : (
                projects.map((p) => (
                  <TableRow key={p.id}>
                    <TableCell className="font-medium">{p.name}</TableCell>
                    <TableCell className="text-muted-foreground">{p.domain}</TableCell>
                    <TableCell className="font-mono text-sm">{p.default_phone}</TableCell>
                    <TableCell>
                      <code className="text-xs text-muted-foreground font-mono">
                        {p.api_key.slice(0, 12)}...
                      </code>
                    </TableCell>
                    <TableCell>
                      <Badge variant={p.is_active ? "answered" : "noAnswer"}>
                        {p.is_active ? "Да" : "Нет"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Button
                        size="sm"
                        variant="outline"
                        className="gap-2"
                        onClick={() => selectProject(p.id)}
                      >
                        <CheckCircle size={13} />
                        Выбрать
                      </Button>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      {/* JS Snippet */}
      {projects.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold">JS Snippet</h2>
          <Card>
            <CardContent className="pt-4">
              <code className="text-xs text-emerald-400 font-mono whitespace-pre-wrap">
                {`<script src="https://YOUR_SERVER/kurotrack.js"\n  data-api="https://YOUR_SERVER/api/v1"\n  data-key="${projects[0]?.api_key || "YOUR_API_KEY"}"\n  data-selector=".kt-phone">\n</script>`}
              </code>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
