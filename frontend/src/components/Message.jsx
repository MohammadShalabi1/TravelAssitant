import styles from './Message.module.css'

const TOOL_LABELS = {
  get_coordinates:    '📍 Geocoding',
  get_current_weather:'🌤 Weather API',
  get_nearby_places:  '🗺 Places API',
}

export default function Message({ role, text, tools_used = [], cached = false }) {
  const isUser = role === 'user'

  return (
    <div className={`${styles.row} ${isUser ? styles.user : ''}`}>
      <div className={`${styles.avatar} ${isUser ? styles.avatarUser : styles.avatarBot}`}>
        {isUser ? '✈️' : '🤖'}
      </div>

      <div className={`${styles.bubble} ${isUser ? styles.bubbleUser : styles.bubbleBot}`}>
        {/* badges — only on assistant messages */}
        {!isUser && (
          <div className={styles.badges}>
            {cached && (
              <span className={styles.badgeCached}>⚡ Cached</span>
            )}
            {tools_used.map(t => (
              <span key={t} className={styles.badgeTool}>
                {TOOL_LABELS[t] ?? t}
              </span>
            ))}
          </div>
        )}

        <p className={styles.text}>{text}</p>
      </div>
    </div>
  )
}
