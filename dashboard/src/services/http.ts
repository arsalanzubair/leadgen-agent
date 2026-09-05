/**
 * http.ts -- the one fetch wrapper.
 *
 * Both real implementations go through this, so error handling, JSON parsing
 * and the "the service is not running" case are written once.
 */

import { ApiError, ServiceOfflineError } from "./types";

/** Where the API lives. Same-origin `/api` by default, so a reverse proxy works. */
export const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

function toQuery(params?: object): string {
  if (!params) return "";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "" || value === "all") continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

async function request<T>(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  options: { query?: object; body?: unknown } = {},
): Promise<T> {
  const url = `${API_BASE}${path}${toQuery(options.query)}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: options.body ? { "Content-Type": "application/json" } : undefined,
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
  } catch {
    // A network-level failure against our own origin means the process is not
    // up. That has a specific remedy, so it gets a specific error.
    throw new ServiceOfflineError(path);
  }

  if (response.status === 204) return undefined as T;

  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }

  if (!response.ok) {
    const detail =
      (payload as { detail?: string; message?: string })?.detail ??
      (payload as { message?: string })?.message ??
      `The request failed (${response.status}).`;
    const remedy = (payload as { remedy?: string })?.remedy;
    throw new ApiError(detail, response.status, remedy);
  }

  return payload as T;
}

export const http = {
  get: <T>(path: string, query?: object) => request<T>("GET", path, { query }),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, { body }),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, { body }),
  del: <T>(path: string) => request<T>("DELETE", path),
};
