import { useEffect, useRef } from "react";
import { useAuth } from "../lib/AuthContext";
import { useChat } from "../hooks/useChat";
import { useSessions } from "../hooks/useSessions";
import Sidebar from "../components/Sidebar";
import Message from "../components/Message";
import TypingIndicator from "../components/TypingIndicator";
import ChatInput from "../components/ChatInput";
import WelcomeScreen from "../components/WelcomeScreen";

export default function ChatPage() {
  const { logout } = useAuth();
  const { messages, loading, error, rateLimitSeconds, send, loadHistory, clearMessages } = useChat();
  const { sessions, activeId, loadingSessions, setActiveId, fetchSessions, createNew, remove, rename } = useSessions();
  const bottomRef = useRef(null);

  // Load sessions on mount
  useEffect(() => { fetchSessions(); }, [fetchSessions]);

  // When active session changes, load its history
  useEffect(() => {
    if (activeId) loadHistory(activeId);
    else clearMessages();
  }, [activeId]);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function handleNew() {
    const id = await createNew();
    clearMessages();
    setActiveId(id);
  }

  async function handleSelect(id) {
    setActiveId(id);
  }

  async function handleSend(text) {
    if (!activeId) {
      // Auto-create session on first message
      const id = await createNew();
      setActiveId(id);
      await send(id, text);
    } else {
      await send(activeId, text);
    }
    // Refresh sidebar to reflect new activity
    fetchSessions();
  }

  function handleSuggestion(text) {
    handleSend(text);
  }

  return (
    <div className="app-layout">
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        loading={loadingSessions}
        onSelect={handleSelect}
        onNew={handleNew}
        onDelete={remove}
        onRename={rename}
        onLogout={logout}
      />

      <main className="chat-area">
        <div className="messages-scroll">
          <div className="messages-inner">
            {messages.length === 0 && !loading && (
              <WelcomeScreen onSuggestion={handleSuggestion} />
            )}

            {messages.map((msg, i) => (
              <Message key={i} {...msg} />
            ))}

            {loading && <TypingIndicator />}

            {error && !loading && (
              <div className="error-toast">{error}</div>
            )}

            <div ref={bottomRef} />
          </div>
        </div>

        <ChatInput
          onSend={handleSend}
          disabled={loading}
          rateLimitSeconds={rateLimitSeconds}
        />
      </main>
    </div>
  );
}
