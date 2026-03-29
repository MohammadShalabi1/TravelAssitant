import { useEffect, useRef } from 'react'
import Message from './Message'
import TypingIndicator from './TypingIndicator'
import Welcome from './Welcome'
import styles from './ChatWindow.module.css'

export default function ChatWindow({ messages, loading, onSuggestion }) {
  const bottomRef = useRef(null)

  // Auto-scroll to bottom on every new message or loading state change
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  const isEmpty = messages.length === 0 && !loading

  return (
    <div className={styles.window}>
      {isEmpty ? (
        <Welcome onSuggestion={onSuggestion} />
      ) : (
        <>
          {messages.map(msg => (
            <Message
              key={msg.id}
              role={msg.role}
              text={msg.text}
              tools_used={msg.tools_used}
              cached={msg.cached}
            />
          ))}
          {loading && <TypingIndicator />}
        </>
      )}
      <div ref={bottomRef} />
    </div>
  )
}
