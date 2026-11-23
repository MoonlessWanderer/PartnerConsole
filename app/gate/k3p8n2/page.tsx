"use client";

import { useEffect } from "react";
import { LoginForm } from "@/components/LoginForm";

export default function AdminEntryPage() {
  useEffect(() => {
    const path = window.location.pathname;
    if (path.startsWith("/") && !path.startsWith("//")) {
      sessionStorage.setItem("pp_admin_entry", path);
    }
  }, []);
  return <LoginForm role="admin" />;
}
