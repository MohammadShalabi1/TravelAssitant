const BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

let accessToken = null;

export function setAccessToken(token) {
  accessToken = token || null;
}

function getCookie(name) {
  const prefix = `${name}=`;
  const cookie = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : null;
}

function authHeaders(includeAuth = true) {
  const headers = { "Content-Type": "application/json" };
  if (includeAuth && accessToken) headers.Authorization = `Bearer ${accessToken}`;
  return headers;
}

async function handleResponse(res) {
  if (!res.ok) {
    const err = await res.json().catch(() => ({ message: "Unknown error" }));
    const error = new Error(err.detail?.message || err.detail || err.message || "Request failed");
    error.status = res.status;
    error.detail = err.detail;
    throw error;
  }

  if (res.status === 204) return null;
  return res.json();
}

async function request(method, path, body = null, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers: authHeaders(options.auth !== false),
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  if (res.status === 401 && options.retry !== false && options.auth !== false) {
    const refreshed = await refreshAuth().catch(() => null);
    if (refreshed?.access_token) {
      const retry = await fetch(`${BASE_URL}${path}`, {
        method,
        headers: authHeaders(true),
        credentials: "include",
        body: body ? JSON.stringify(body) : undefined,
      });
      return handleResponse(retry);
    }
  }

  return handleResponse(res);
}

export async function refreshAuth() {
  const csrfToken = getCookie("csrf_token");
  if (!csrfToken) return null;
  const res = await fetch(`${BASE_URL}/api/auth/refresh`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    credentials: "include",
  });
  const data = await handleResponse(res);
  setAccessToken(data.access_token);
  return data;
}

export async function logoutRequest() {
  const csrfToken = getCookie("csrf_token");
  if (csrfToken) {
    await fetch(`${BASE_URL}/api/auth/logout`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      credentials: "include",
    }).catch(() => null);
  }
  setAccessToken(null);
}

export const register = async (email, password) => {
  const data = await request("POST", "/api/auth/register", { email, password }, { auth: false });
  setAccessToken(data.access_token);
  return data;
};

export const login = async (email, password) => {
  const data = await request("POST", "/api/auth/login", { email, password }, { auth: false });
  setAccessToken(data.access_token);
  return data;
};

export const createSession = () => request("POST", "/api/sessions");
export const listSessions = () => request("GET", "/api/sessions");
export const deleteSession = (id) => request("DELETE", `/api/sessions/${id}`);
export const renameSession = (id, name) =>
  request("PATCH", `/api/sessions/${id}/rename`, { name });

export const sendMessage = (session_id, message) =>
  request("POST", "/api/chat", { session_id, message });

export const getHistory = (session_id, limit = 50, offset = 0) =>
  request("GET", `/api/sessions/${session_id}/history?limit=${limit}&offset=${offset}`);

export const exportSession = async (session_id) => {
  const res = await fetch(`${BASE_URL}/api/sessions/${session_id}/export`, {
    headers: authHeaders(true),
    credentials: "include",
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `chat-${session_id.slice(0, 8)}.json`;
  a.click();
  URL.revokeObjectURL(url);
};

export const getHealth = () => request("GET", "/health", null, { auth: false });
