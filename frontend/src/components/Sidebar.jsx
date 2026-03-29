import styles from './Sidebar.module.css'

export default function Sidebar({ sessions = [], activeId, onNew, onSelect }) {

  // Format session label: use first 30 chars of session_id or a timestamp
  function formatLabel(session) {
    if (session.label) return session.label
    // Fall back to a readable date from created_at
    if (session.created_at) {
      const d = new Date(session.created_at)
      return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    }
    // Last resort: short session ID
    return session.session_id.slice(0, 8) + '...'
  }

  return (
    <aside className={styles.sidebar}>
      <div className={styles.logo}>
        🌐 Wander<span className={styles.dot}>AI</span>
      </div>

      <span className={styles.label}>Sessions</span>

      <nav className={styles.nav}>
        {sessions.length === 0 && (
          <p className={styles.empty}>No sessions yet</p>
        )}
        {sessions.map(s => (
          <button
            key={s.session_id}
            className={`${styles.item} ${activeId === s.session_id ? styles.active : ''}`}
            onClick={() => onSelect(s.session_id)}
          >
            <span className={styles.itemDot} />
            {formatLabel(s)}
          </button>
        ))}
      </nav>

      <button className={styles.newBtn} onClick={onNew}>
        ＋ New session
      </button>
    </aside>
  )
}
