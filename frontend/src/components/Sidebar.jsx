import { useState, useEffect, useRef } from "react";
import * as api from "../lib/api";

function formatDate(str) {
  const d = new Date(str);
  const now = new Date();
  const diffDays = Math.floor((now - d) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return d.toLocaleDateString("en", { weekday: "long" });
  return d.toLocaleDateString("en", { month: "short", day: "numeric" });
}

function SessionItem({ session, isActive, onSelect, onDelete, onRename, onExport }) {
  const [editing, setEditing] = useState(false);
  const [nameVal, setNameVal] = useState(session.name || "");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  useEffect(() => {
    function close(e) { if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false); }
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  function submitRename(e) {
    e.preventDefault();
    if (nameVal.trim()) onRename(session.session_id, nameVal.trim());
    setEditing(false);
  }

  const label = session.name || `Chat · ${session.session_id.slice(0, 8)}`;

  return (
    <div className={`session-item ${isActive ? "active" : ""}`} onClick={() => onSelect(session.session_id)}>
      {editing ? (
        <form onSubmit={submitRename} onClick={(e) => e.stopPropagation()}>
          <input
            ref={inputRef}
            className="rename-input"
            value={nameVal}
            onChange={(e) => setNameVal(e.target.value)}
            onBlur={submitRename}
            maxLength={60}
          />
        </form>
      ) : (
        <span className="session-label">{label}</span>
      )}
      <span className="session-date">{formatDate(session.created_at)}</span>

      <div className="session-menu-wrap" ref={menuRef} onClick={(e) => e.stopPropagation()}>
        <button className="session-menu-btn" onClick={() => setMenuOpen((o) => !o)}>⋯</button>
        {menuOpen && (
          <div className="session-menu">
            <button onClick={() => { setEditing(true); setMenuOpen(false); }}>Rename</button>
            <button onClick={() => { onExport(session.session_id); setMenuOpen(false); }}>Export JSON</button>
            <button className="danger" onClick={() => { onDelete(session.session_id); setMenuOpen(false); }}>Delete</button>
          </div>
        )}
      </div>
    </div>
  );
}

export default function Sidebar({ sessions, activeId, loading, onSelect, onNew, onDelete, onRename, onLogout }) {
  async function handleExport(id) {
    await api.exportSession(id);
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <span className="logo-icon">✈</span>
          <span className="logo-name">Rihla</span>
        </div>
        <button className="new-chat-btn" onClick={onNew} title="New conversation">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        </button>
      </div>

      <div className="sidebar-sessions">
        {loading && <div className="sessions-loading"><span className="dot-pulse" /></div>}
        {!loading && sessions.length === 0 && (
          <p className="sessions-empty">No conversations yet.<br/>Start one above.</p>
        )}
        {sessions.map((s) => (
          <SessionItem
            key={s.session_id}
            session={s}
            isActive={s.session_id === activeId}
            onSelect={onSelect}
            onDelete={onDelete}
            onRename={onRename}
            onExport={handleExport}
          />
        ))}
      </div>

      <div className="sidebar-footer">
        <button className="logout-btn" onClick={onLogout}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/>
            <polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>
          </svg>
          Sign out
        </button>
      </div>
    </aside>
  );
}
