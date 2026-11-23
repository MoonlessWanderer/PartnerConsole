"use client";

import { FormEvent, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { Banner, Button, Field, TableWrap, inputClass } from "@/components/ui";
import { Modal } from "@/components/Modal";

type Panel = {
  id: number;
  name: string;
  base_url: string;
  secure_path: string;
  admin_email: string;
  status: string;
  last_ok_jalali: string | null;
};

const empty = { name: "", base_url: "", secure_path: "", admin_email: "", admin_password: "", status: "active" };

export default function PanelsPage() {
  const [items, setItems] = useState<Panel[]>([]);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Panel | null>(null);
  const [form, setForm] = useState(empty);

  async function load() {
    const data = await api<{ items: Panel[] }>("/admin/panels");
    setItems(data.items);
  }
  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, []);

  function startCreate() {
    setEditing(null);
    setForm(empty);
    setOpen(true);
  }
  function startEdit(panel: Panel) {
    setEditing(panel);
    setForm({ ...panel, admin_password: "", status: panel.status });
    setOpen(true);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      if (editing) {
        await api(`/admin/panels/${editing.id}`, { method: "PUT", body: { ...form, admin_password: form.admin_password || null } });
      } else {
        await api("/admin/panels", { method: "POST", body: form });
      }
      setOpen(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "خطا");
    }
  }

  async function test(id: number) {
    setError("");
    setInfo("");
    try {
      const data = await api<{ plan_count: number }>(`/admin/panels/${id}/test`, { method: "POST" });
      setInfo(`اتصال برقرار شد. ${data.plan_count} پلن خوانده شد.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "تست ناموفق بود");
      await load().catch(() => undefined);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-bold">پنل‌های V2Board</h2>
        <Button onClick={startCreate}>پنل جدید</Button>
      </div>
      {error && !open ? <Banner>{error}</Banner> : null}
      {info ? <Banner tone="ok">{info}</Banner> : null}
      <TableWrap>
        <table>
          <thead>
            <tr>
              <th>نام</th>
              <th>آدرس</th>
              <th>مسیر امن</th>
              <th>وضعیت</th>
              <th>آخرین تست</th>
              <th>عملیات</th>
            </tr>
          </thead>
          <tbody>
            {items.map((panel) => (
              <tr key={panel.id}>
                <td>{panel.name}</td>
                <td>{panel.base_url}</td>
                <td>{panel.secure_path}</td>
                <td>{panel.status === "active" ? "فعال" : panel.status === "disabled" ? "خاموش" : "خطا"}</td>
                <td>{formatDate(panel.last_ok_jalali) || "تست نشده"}</td>
                <td className="space-x-2 space-x-reverse">
                  <Button tone="soft" onClick={() => test(panel.id)}>تست اتصال</Button>
                  <Button tone="ghost" onClick={() => startEdit(panel)}>ویرایش</Button>
                </td>
              </tr>
            ))}
            {items.length === 0 ? (
              <tr><td colSpan={6}>هنوز پنلی ثبت نشده.</td></tr>
            ) : null}
          </tbody>
        </table>
      </TableWrap>
      {open ? (
        <Modal title={editing ? "ویرایش پنل" : "پنل جدید"} error={error} onClose={() => setOpen(false)}>
          <form className="space-y-3" onSubmit={save}>
            <Field label="نام"><input className={inputClass} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></Field>
            <Field label="آدرس"><input className={inputClass} value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="https://panel.example.com" required /></Field>
            <Field label="مسیر امن ادمین"><input className={inputClass} value={form.secure_path} onChange={(e) => setForm({ ...form, secure_path: e.target.value })} required /></Field>
            <Field label="ایمیل ادمین پنل"><input className={inputClass} value={form.admin_email} onChange={(e) => setForm({ ...form, admin_email: e.target.value })} required /></Field>
            <Field label={editing ? "رمز جدید (خالی = بدون تغییر)" : "رمز ادمین پنل"}>
              <input className={inputClass} type="password" value={form.admin_password} onChange={(e) => setForm({ ...form, admin_password: e.target.value })} required={!editing} />
            </Field>
            <Field label="وضعیت">
              <select className={inputClass} value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })}>
                <option value="active">فعال</option>
                <option value="disabled">خاموش</option>
              </select>
            </Field>
            <Button type="submit" className="w-full">ذخیره</Button>
          </form>
        </Modal>
      ) : null}
    </div>
  );
}
