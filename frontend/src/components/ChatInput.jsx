import { useState, useRef, useEffect } from "react";

export default function ChatInput({ onSend, disabled, rateLimitSeconds, maxLength = 1000 }) {
  const [value, setValue] = useState("");
  const textareaRef = useRef(null);

  useEffect(() => {
    if (!disabled) textareaRef.current?.focus();
  }, [disabled]);

  function autoResize() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || rateLimitSeconds > 0) return;
    onSend(trimmed);
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }

  const charsLeft = maxLength - value.length;
  const isOverLimit = charsLeft < 0;

  return (
    <div className="chat-input-wrap">
      {rateLimitSeconds > 0 && (
        <div className="rate-notice">
          <span className="rate-icon">⏱</span>
          Please wait {rateLimitSeconds}s before sending again
        </div>
      )}
      <div className={`chat-input-box ${disabled ? "disabled" : ""}`}>
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => { setValue(e.target.value); autoResize(); }}
          onKeyDown={handleKeyDown}
          placeholder="Ask about weather, places, travel tips…"
          disabled={disabled || rateLimitSeconds > 0}
          rows={1}
          maxLength={maxLength + 50} // let them see they're over
        />
        <div className="input-actions">
          {value.length > maxLength * 0.8 && (
            <span className={`char-count ${isOverLimit ? "over" : ""}`}>
              {charsLeft}
            </span>
          )}
          <button
            className="send-btn"
            onClick={submit}
            disabled={disabled || rateLimitSeconds > 0 || !value.trim() || isOverLimit}
            title="Send (Enter)"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
              <line x1="22" y1="2" x2="11" y2="13"/>
              <polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
          </button>
        </div>
      </div>
      <p className="input-hint">Enter to send · Shift+Enter for new line</p>
    </div>
  );
}
