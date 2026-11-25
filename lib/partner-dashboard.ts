"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Product } from "@/lib/types";

type Dashboard = { balance_toman: number; subscription_count: number; credits: Product[] };

export function usePartnerDashboard() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api<Dashboard>("/partner/dashboard").then(result => {
      if (active) setData(result);
    }).catch((err: unknown) => {
      if (active) setError(err instanceof ApiError && err.message.trim() ? err.message : "بارگیری اطلاعات حساب انجام نشد. دوباره تلاش کنید.");
    });
    return () => { active = false; };
  }, []);
  return {data, error};
}
