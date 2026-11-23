"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { LoginForm } from "@/components/LoginForm";

export default function HomePage() {
  const router = useRouter();
  useEffect(() => {
    if (localStorage.getItem("pp_token") && localStorage.getItem("pp_role") === "partner") {
      router.replace("/partner");
    }
  }, [router]);
  return <LoginForm role="partner" />;
}
