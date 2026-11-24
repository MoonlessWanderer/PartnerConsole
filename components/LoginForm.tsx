"use client";

import { FormEvent, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, saveSession } from "@/lib/api";
import { Banner, Button, Field, inputClass } from "@/components/ui";
import { BrandMark, Icon, IconTile } from "@/components/Icons";

export function LoginForm({ role }: { role: "admin" | "partner" }) {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [visible, setVisible] = useState(false);
  const submitting = useRef(false);
  const admin = role === "admin";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setPending(true);
    setError("");
    try {
      const data = await api<{ access_token: string; username: string }>(`/${role}/auth/login`, {
        method: "POST",
        body: { username, password },
      });
      saveSession(data.access_token, role, data.username);
      router.replace(admin ? "/admin" : "/partner");
    } catch (err) {
      setError(err instanceof Error ? err.message : "ورود ناموفق بود");
    } finally {
      setPending(false);
      submitting.current = false;
    }
  }

  return <main className="pc-login">
    <div className="pc-login-logo"><BrandMark /></div>
    <form onSubmit={submit} className="pc-card pc-login-form">
      <div className="pc-login-heading">
        <IconTile name={admin ? "shield" : "user"} />
        <h1>{admin ? "ورود مدیر" : "ورود همکار"}</h1>
        <p className="pc-quiet">برای دسترسی به حساب خود وارد شوید.</p>
      </div>
      {error && <Banner>{error}</Banner>}
      <div className="pc-login-field">
        <Field label="نام کاربری">
          <input className={inputClass} dir="ltr" value={username} placeholder="نام کاربری" onChange={e => setUsername(e.target.value)} autoComplete="username" />
        </Field>
        <span className="pc-login-affix" aria-hidden="true"><Icon name="user" /></span>
      </div>
      <div className="pc-login-field">
        <Field label="رمز عبور">
          <input className={inputClass} dir="ltr" type={visible ? "text" : "password"} value={password}
            onChange={e => setPassword(e.target.value)} autoComplete="current-password" />
        </Field>
        <button type="button" className="pc-login-affix" onClick={() => setVisible(value => !value)}
          aria-label={visible ? "پنهان‌کردن رمز عبور" : "نمایش رمز عبور"}>
          <Icon name={visible ? "eyeOff" : "eye"} />
        </button>
      </div>
      <Button type="submit" label={pending ? "در حال ورود…" : "ورود"} disabled={pending} className="w-full">
        <Icon name="left" />{pending ? "در حال ورود…" : "ورود به حساب"}
      </Button>
    </form>
    <div className="pc-login-footer"><bdi dir="ltr">PartnerConsole</bdi><span aria-hidden="true">·</span> دسترسی امن</div>
  </main>;
}
