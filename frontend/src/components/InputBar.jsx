import { useState, useRef } from 'react'
import styles from './InputBar.module.css'

export default function InputBar({ onSend, disabled, rateLimited, countdown }) {
  const [value, setValue] = useState('')
  const textareaRef = useRef(null)

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  function submit() {
    const text = value.trim()
    if (!text || disabled) return
    onSend(text)
    setValue('')
    // reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  function handleChange(e) {
    setValue(e.target.value)
    // auto-grow
    e.target.style.height = 'auto'
    e.target.style.height = e.target.scrollHeight + 'px'
  }

  return (
    <div className={styles.wrap}>
      {rateLimited && (
        <div className={styles.rateBanner}>
          ⏳ Rate limited — wait <strong className={styles.count}>{countdown}s</strong> before sending again.
        </div>
      )}

      <div className={`${styles.bar} ${disabled ? styles.barDisabled : ''}`}>
        <textarea
          ref={textareaRef}
          className={styles.input}
          rows={1}
          placeholder="Ask about weather, restaurants, attractions…"
          value={value}
          onChange={handleChange}
          onKeyDown={handleKey}
          disabled={disabled}
        />
        <button
          className={styles.sendBtn}
          onClick={submit}
          disabled={!value.trim() || disabled}
        >
          ↑
        </button>
      </div>

      <p className={styles.hint}>Enter to send · Shift+Enter for new line</p>
    </div>
  )
}
