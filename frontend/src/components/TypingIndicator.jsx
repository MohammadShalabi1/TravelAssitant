import styles from './TypingIndicator.module.css'

export default function TypingIndicator() {
  return (
    <div className={styles.row}>
      <div className={styles.avatar}>🤖</div>
      <div className={styles.bubble}>
        <span /><span /><span />
      </div>
    </div>
  )
}
