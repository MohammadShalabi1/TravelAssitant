import styles from './Welcome.module.css'

const SUGGESTIONS = [
  "What's the weather in Tokyo?",
  "Find restaurants near Paris",
  "Things to do in Beirut",
  "Weather in New York today",
]

export default function Welcome({ onSuggestion }) {
  return (
    <div className={styles.wrap}>
      <div className={styles.icon}>🌍</div>
      <h2 className={styles.title}>Where to next?</h2>
      <p className={styles.sub}>
        Ask me about weather, restaurants, or attractions anywhere in the world.
      </p>
      <div className={styles.chips}>
        {SUGGESTIONS.map(s => (
          <button key={s} className={styles.chip} onClick={() => onSuggestion(s)}>
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}
