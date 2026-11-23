"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { api, saveSession } from "@/lib/api";
import { Banner, Button, Field, inputClass } from "@/components/ui";

export function LoginForm({ role }: { role: "admin" | "partner" }) {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      const data = await api<{ access_token: string; username: string }>(`/${role}/auth/login`, {
        method: "POST",
        body: { username, password },
      });
      saveSession(data.access_token, role, data.username);
      router.replace(role === "admin" ? "/admin" : "/partner");
    } catch (err) {
      setError(err instanceof Error ? err.message : "ورود ناموفق بود");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center p-4">
      <form onSubmit={submit} className="space-y-4 rounded-2xl bg-white p-5 shadow-sm">
        <div>
          <div className="text-sm text-slate-500">{role === "admin" ? "مدیر سیستم" : "Partner Console"}</div>
          <h1 className="text-2xl font-bold">ورود</h1>
        </div>
        {error ? <Banner>{error}</Banner> : null}
        <Field label="نام کاربری">
          <input className={inputClass} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        </Field>
        <Field label="رمز عبور">
          <input className={inputClass} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </Field>
        <Button type="submit" disabled={pending} className="w-full">
          {pending ? "در حال ورود…" : "ورود"}
        </Button>
      </form>
    </main>
  );
}
