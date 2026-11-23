export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8741";
const REQUEST_TIMEOUT_MS = 45_000;
const RESPONSE_ERROR = "پاسخ سرور قابل خواندن نیست.";

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function saveSession(token: string, role: string, username: string) {
  localStorage.setItem("pp_token", token);
  localStorage.setItem("pp_role", role);
  localStorage.setItem("pp_username", username);
}

export function clearSession() {
  localStorage.removeItem("pp_token");
  localStorage.removeItem("pp_role");
  localStorage.removeItem("pp_username");
}

export function sessionToken() {
  return typeof window === "undefined" ? null : localStorage.getItem("pp_token");
}

export function sessionRole() {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("pp_role");
}

export function sessionUsername() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("pp_username") || "";
}

async function requestWithDeadline<T>(
  path: string,
  options: RequestInit,
  read: (response: Response) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const write = !["GET", "HEAD"].includes(options.method || "GET");
  const uncertain = "نتیجه درخواست مشخص نیست. پیش از تلاش دوباره، وضعیت را بررسی کنید.";
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        reject(new ApiError(write ? uncertain : "مهلت دریافت پاسخ سرور تمام شد. دوباره تلاش کنید.", 0));
        controller.abort();
      }, REQUEST_TIMEOUT_MS);
    });
    const request = fetch(`${API_BASE}/api${path}`, { ...options, signal: controller.signal }).then((response) => {
      controller.signal.throwIfAborted();
      return read(response);
    });
    return await Promise.race([request, deadline]);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(write ? uncertain : "ارتباط با سرور برقرار نشد. دوباره تلاش کنید.", 0);
  } finally {
    clearTimeout(timer);
  }
}

export async function logoutSession(role: "admin" | "partner"): Promise<void> {
  const token = typeof window === "undefined" ? null : localStorage.getItem("pp_token");
  await requestWithDeadline(`/${role}/auth/logout`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  }, async (response) => {
    if (response.ok || response.status === 401 || response.status === 403) return;
    throw new ApiError("خروج انجام نشد. دوباره تلاش کنید.", response.status);
  });
}

export async function api<T>(path: string, options: { method?: string; body?: unknown; reviewSession?: string | null } = {}): Promise<T> {
  const token = typeof window === "undefined" ? null : localStorage.getItem("pp_token");
  if (options.reviewSession !== undefined && options.reviewSession !== token) {
    throw new ApiError("نشست تغییر کرده است. صفحه را دوباره باز کنید.", 409);
  }
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  const method = options.method || "GET";
  return requestWithDeadline(path, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  }, async (response) => {
    const isLogin = path.includes("/auth/login");
    if (response.status === 401 && token && !isLogin && sessionToken() === token) {
      const role = localStorage.getItem("pp_role");
      clearSession();
      const stored = sessionStorage.getItem("pp_admin_entry");
      const adminEntry = stored && stored.startsWith("/") && !stored.startsWith("//") ? stored : "";
      window.location.href = role === "admin" && adminEntry ? adminEntry : "/";
    }
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const publicCode = response.status < 500
        ? ["request_rejected", "invalid_input", "http_error", "body_too_large", "financial_consent"].includes(data?.code)
        : ["operation_status", "panel_timeout", "panel_unavailable"].includes(data?.code);
      const detail = publicCode && typeof data?.detail === "string"
        ? data.detail : "دریافت پاسخ معتبر از سرور ممکن نشد. وضعیت درخواست را بررسی کنید.";
      throw new ApiError(detail, response.status, publicCode ? data.code : undefined);
    }
    if (!data || typeof data !== "object") {
      const detail = method === "GET" ? RESPONSE_ERROR
        : "پاسخ سرور قابل خواندن نیست. پیش از تلاش دوباره، وضعیت عملیات را بررسی کنید.";
      throw new ApiError(detail, 502);
    }
    return data as T;
  });
}
