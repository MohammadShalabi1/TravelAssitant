/**
 * api/client.js
 *
 * Every call to the FastAPI backend lives here.
 * Components never call fetch() directly — they import from this file.
 * This makes it trivial to swap the base URL or add auth headers later.
 */

const BASE = '/api'   // Vite proxy forwards this to http://localhost:8000

// ── helpers ──────────────────────────────────────────────────────────────────

async function handleResponse(res) {
  const data = await res.json()

  if (!res.ok) {
    // FastAPI error shape: { detail: "..." } or { detail: { error, retry_after_seconds } }
    const detail = data?.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.error ?? `HTTP ${res.status}`

    const err = new Error(message)
    err.status = res.status
    err.detail = detail           // keep full detail so callers can inspect it
    throw err
  }

  return data
}

// ── endpoints ─────────────────────────────────────────────────────────────────

/**
 * POST /api/sessions
 * Creates a new conversation session.
 * @returns {Promise<{ session_id: string }>}
 */
export async function createSession() {
  const res = await fetch(`${BASE}/sessions`, { method: 'POST' })
  return handleResponse(res)
}

/**
 * POST /api/chat
 * Sends a message and returns the assistant reply.
 * @param {string} sessionId
 * @param {string} message
 * @returns {Promise<{ text: string, tools_used: string[], cached: boolean, session_id: string }>}
 */
export async function sendMessage(sessionId, message) {
  const res = await fetch(`${BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, message }),
  })
  return handleResponse(res)
}

/**
 * GET /api/sessions/:id/history
 * Loads the message history for an existing session.
 * @param {string} sessionId
 * @returns {Promise<{ session_id: string, messages: { role: string, content: string }[] }>}
 */
export async function fetchHistory(sessionId) {
  const res = await fetch(`${BASE}/sessions/${sessionId}/history`)
  return handleResponse(res)
}

/**
 * GET /api/sessions
 * Returns all past sessions so the sidebar can list them.
 * @returns {Promise<{ sessions: { session_id: string, label: string, created_at: string }[] }>}
 */
export async function fetchSessions() {
  const res = await fetch(`${BASE}/sessions`)
  return handleResponse(res)
}
