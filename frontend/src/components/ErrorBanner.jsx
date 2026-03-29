import styles from './ErrorBanner.module.css'

export default function ErrorBanner({ message, onDismiss }) {
  if (!message) return null

  return (
    <div className={styles.banner}>
      <span className={styles.icon}>⚠️</span>
      <span className={styles.text}>{message}</span>
      <button className={styles.close} onClick={onDismiss}>✕</button>
    </div>
  )
}
