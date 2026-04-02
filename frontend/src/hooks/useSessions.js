import { useState, useCallback } from "react";
import * as api from "../lib/api";

export function useSessions() {
  const [sessions, setSessions] = useState([]);
  const [activeId, setActiveId] = useState(null);
  const [loadingSessions, setLoadingSessions] = useState(false);

  const fetchSessions = useCallback(async () => {
    setLoadingSessions(true);
    try {
      const data = await api.listSessions();
      setSessions(data.sessions || []);
    } catch (e) {
      console.error("Failed to load sessions", e);
    } finally {
      setLoadingSessions(false);
    }
  }, []);

  const createNew = useCallback(async () => {
    const data = await api.createSession();
    await fetchSessions();
    setActiveId(data.session_id);
    return data.session_id;
  }, [fetchSessions]);

  const remove = useCallback(async (id) => {
    await api.deleteSession(id);
    setSessions((prev) => prev.filter((s) => s.session_id !== id));
    if (activeId === id) setActiveId(null);
  }, [activeId]);

  const rename = useCallback(async (id, name) => {
    await api.renameSession(id, name);
    setSessions((prev) =>
      prev.map((s) => s.session_id === id ? { ...s, name } : s)
    );
  }, []);

  return {
    sessions, activeId, loadingSessions,
    setActiveId, fetchSessions, createNew, remove, rename,
  };
}
