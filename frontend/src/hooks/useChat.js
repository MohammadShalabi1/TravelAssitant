import { useState, useCallback, useRef } from "react";
import * as api from "../lib/api";

export function useChat() {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [rateLimitSeconds, setRateLimitSeconds] = useState(0);
  const timerRef = useRef(null);

  function startRateTimer(seconds) {
    setRateLimitSeconds(seconds);
    clearInterval(timerRef.current);
    timerRef.current = setInterval(() => {
      setRateLimitSeconds((s) => {
        if (s <= 1) { clearInterval(timerRef.current); return 0; }
        return s - 1;
      });
    }, 1000);
  }

  const loadHistory = useCallback(async (sessionId) => {
    try {
      const data = await api.getHistory(sessionId);
      setMessages(data.messages || []);
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const send = useCallback(async (sessionId, text) => {
    if (!text.trim() || loading) return;

    const userMsg = { role: "user", content: text };
    setMessages((prev) => [...prev, userMsg]);
    setLoading(true);
    setError(null);

    try {
      const data = await api.sendMessage(sessionId, text);
      const assistantMsg = {
        role: "assistant",
        content: data.text,
        tools_used: data.tools_used,
        cached: data.cached,
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch (e) {
      if (e.status === 429) {
        const secs = e.detail?.retry_after_seconds || 5;
        startRateTimer(secs);
        setMessages((prev) => prev.slice(0, -1)); // remove optimistic user msg
        setError(`Please wait ${secs}s before sending again.`);
      } else {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: "Something went wrong. Please try again.", isError: true },
        ]);
        setError(e.message);
      }
    } finally {
      setLoading(false);
    }
  }, [loading]);

  function clearMessages() {
    setMessages([]);
    setError(null);
  }

  return { messages, loading, error, rateLimitSeconds, send, loadHistory, clearMessages };
}
