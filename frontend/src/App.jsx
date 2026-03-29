import { useState } from 'react'
import Sidebar from './components/Sidebar'
import Topbar from './components/Topbar'
import ChatWindow from './components/ChatWindow'
import InputBar from './components/InputBar'
import ErrorBanner from './components/ErrorBanner'
import { useChat } from './hooks/useChat'
import styles from './App.module.css'

export default function App() {
  const [activeSessionId, setActiveSessionId] = useState(null)

  const {
    messages,
    sessions,
    loading,
    error,
    rateLimited,
    countdown,
    newSession,
    loadSession,
    send,
  } = useChat()

  async function handleNew() {
    setActiveSessionId(null)
    await newSession()
  }

  async function handleSelect(sessionId) {
    setActiveSessionId(sessionId)
    await loadSession(sessionId)
  }

  return (
    <div className={styles.layout}>
      <Sidebar
        sessions={sessions}
        activeId={activeSessionId}
        onNew={handleNew}
        onSelect={handleSelect}
      />

      <div className={styles.main}>
        <Topbar />

        <ChatWindow
          messages={messages}
          loading={loading}
          onSuggestion={send}
        />

        <ErrorBanner
          message={error}
          onDismiss={() => {}}
        />

        <InputBar
          onSend={send}
          disabled={loading}
          rateLimited={rateLimited}
          countdown={countdown}
        />
      </div>
    </div>
  )
}
