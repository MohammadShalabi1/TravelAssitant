import styles from './Topbar.module.css'

export default function Topbar() {
  return (
    <header className={styles.topbar}>
      <div>
        <div className={styles.title}>Travel Assistant</div>
        <div className={styles.sub}>Gemini 2.5 Flash · Semantic Cache · Rate Limiting</div>
      </div>
      <div className={styles.pill}>
        <span className={styles.pulse} />
        Online
      </div>
    </header>
  )
}
