const BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

function getToken() {
  return localStorage.getItem("token");
}

async function request(method, path, body = null) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${BASE_URL}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

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

// Auth
export const register = (email, password) =>
  request("POST", "/api/auth/register", { email, password });

export const login = (email, password) =>
  request("POST", "/api/auth/login", { email, password });

// Sessions
export const createSession = () => request("POST", "/api/sessions");
export const listSessions = () => request("GET", "/api/sessions");
export const deleteSession = (id) => request("DELETE", `/api/sessions/${id}`);
export const renameSession = (id, name) =>
  request("PATCH", `/api/sessions/${id}/rename`, { name });

// Chat
export const sendMessage = (session_id, message) =>
  request("POST", "/api/chat", { session_id, message });

// History
export const getHistory = (session_id, limit = 50, offset = 0) =>
  request("GET", `/api/sessions/${session_id}/history?limit=${limit}&offset=${offset}`);

// Export
export const exportSession = async (session_id) => {
  const token = getToken();
  const res = await fetch(`${BASE_URL}/api/sessions/${session_id}/export`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `chat-${session_id.slice(0, 8)}.json`;
  a.click();
  URL.revokeObjectURL(url);
};

// Health
export const getHealth = () => request("GET", "/health");
