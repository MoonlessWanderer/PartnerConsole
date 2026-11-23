"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { LoginForm } from "@/components/LoginForm";
import { sessionRole, sessionToken } from "@/lib/api";

export default function HomePage() {
  const router = useRouter();
  useEffect(() => {
    function syncSession() {
      if (sessionToken() && sessionRole() === "partner") {
        router.replace("/partner");
      }
    }
    syncSession();
    window.addEventListener("storage", syncSession);
    return () => window.removeEventListener("storage", syncSession);
  }, [router]);
  return <LoginForm role="partner" />;
}
