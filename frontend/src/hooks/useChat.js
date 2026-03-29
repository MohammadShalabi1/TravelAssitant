/**
 * hooks/useChat.js
 *
 * Owns ALL chat state and logic.
 * The UI components just call the functions this hook exposes.
 */

import { useState, useRef, useCallback, useEffect } from 'react'
import { createSession, sendMessage, fetchHistory, fetchSessions } from '../api/client'

const RATE_LIMIT_MS = 5000

export function useChat() {
  const [messages, setMessages]       = useState([])
  const [sessions, setSessions]       = useState([])   // list of past sessions for sidebar
  const [loading, setLoading]         = useState(false)
  const [error, setError]             = useState(null)
  const [rateLimited, setRateLimited] = useState(false)
  const [countdown, setCountdown]     = useState(0)

  const sessionIdRef   = useRef(null)
  const lastSentRef    = useRef(0)
  const countdownTimer = useRef(null)

  // ── Load all sessions on mount so sidebar is populated ───────────────────
  useEffect(() => {
    loadAllSessions()
  }, [])

  async function loadAllSessions() {
    try {
      const data = await fetchSessions()
      setSessions(data.sessions)
    } catch (e) {
      console.warn('Could not load sessions:', e.message)
    }
  }

  // ── helpers ────────────────────────────────────────────────────────────────

  function addMessage(role, text, meta = {}) {
    setMessages(prev => [
      ...prev,
      { id: Date.now() + Math.random(), role, text, tools_used: [], cached: false, ...meta }
    ])
  }

  function startCountdown() {
    setRateLimited(true)
    clearInterval(countdownTimer.current)
    countdownTimer.current = setInterval(() => {
      const remaining = Math.ceil((lastSentRef.current + RATE_LIMIT_MS - Date.now()) / 1000)
      if (remaining <= 0) {
        setRateLimited(false)
        setCountdown(0)
        clearInterval(countdownTimer.current)
      } else {
        setCountdown(remaining)
      }
    }, 200)
  }

  // ── public API ─────────────────────────────────────────────────────────────

  const newSession = useCallback(async () => {
    setMessages([])
    setError(null)
    setRateLimited(false)
    setCountdown(0)
    lastSentRef.current = 0

    try {
      const data = await createSession()
      sessionIdRef.current = data.session_id
      await loadAllSessions()
    } catch (e) {
      setError('Could not start a new session. Is the backend running?')
    }
  }, [])

  /**
   * Load an existing session — fetch history from backend and display it.
   * Called when user clicks a session in the sidebar.
   */
  const loadSession = useCallback(async (sessionId) => {
    setMessages([])
    setError(null)
    setLoading(true)
    sessionIdRef.current = sessionId

    try {
      const data = await fetchHistory(sessionId)

      // Map each DB row { role, content } → shape Message component expects
      const loaded = data.messages.map((m, i) => ({
        id: i,
        role: m.role,       // "user" | "assistant"
        text: m.content,
        tools_used: [],
        cached: false,
      }))

      setMessages(loaded)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  const send = useCallback(async (text) => {
    if (!text.trim() || loading) return

    const now = Date.now()
    if (now - lastSentRef.current < RATE_LIMIT_MS) {
      startCountdown()
      return
    }
    lastSentRef.current = now
    setRateLimited(false)
    setError(null)

    if (!sessionIdRef.current) {
      try {
        const data = await createSession()
        sessionIdRef.current = data.session_id
        await loadAllSessions()
      } catch (e) {
        setError('Could not connect to the backend.')
        return
      }
    }

    addMessage('user', text)
    setLoading(true)

    try {
      const data = await sendMessage(sessionIdRef.current, text)
      addMessage('assistant', data.text, {
        tools_used: data.tools_used,
        cached: data.cached,
      })
    } catch (e) {
      if (e.status === 429) {
        const wait = e.detail?.retry_after_seconds ?? 5
        setCountdown(wait)
        startCountdown()
      } else {
        setError(e.message ?? 'Something went wrong.')
      }
    } finally {
      setLoading(false)
    }
  }, [loading])

  return {
    messages,
    sessions,
    loading,
    error,
    rateLimited,
    countdown,
    sessionId: sessionIdRef.current,
    newSession,
    loadSession,
    send,
  }
}
