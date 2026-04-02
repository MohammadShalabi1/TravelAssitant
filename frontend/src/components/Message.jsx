import { useState } from "react";

// Very lightweight markdown renderer (bold, italic, inline code, code blocks, lists)
function renderMarkdown(text) {
  if (!text) return "";
  return text
    // code blocks
    .replace(/```[\w]*\n?([\s\S]*?)```/g, "<pre><code>$1</code></pre>")
    // inline code
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    // bold
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // italic
    .replace(/\*(.+?)\*/g, "<em>$1</em>")
    // unordered list items
    .replace(/^[\-\*] (.+)$/gm, "<li>$1</li>")
    // numbered list items
    .replace(/^\d+\. (.+)$/gm, "<li>$1</li>")
    // wrap consecutive <li> in <ul>
    .replace(/(<li>.*<\/li>(\n)?)+/g, (m) => `<ul>${m}</ul>`)
    // paragraphs (double newline)
    .replace(/\n\n/g, "</p><p>")
    // single newlines
    .replace(/\n/g, "<br/>");
}

const TOOL_LABELS = {
  get_coordinates: "📍 Geocoding",
  get_current_weather: "🌤 Weather",
  get_nearby_places: "🗺 Places",
};

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }
  return (
    <button className="copy-btn" onClick={copy} title="Copy message">
      {copied ? (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      ) : (
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
        </svg>
      )}
    </button>
  );
}

export default function Message({ role, content, tools_used, cached, isError }) {
  const isUser = role === "user";
  const html = isUser ? null : renderMarkdown(content);

  return (
    <div className={`message ${isUser ? "user" : "assistant"} ${isError ? "error" : ""}`}>
      {!isUser && (
        <div className="avatar">
          <span>✈</span>
        </div>
      )}
      <div className="message-body">
        <div className="bubble">
          {isUser ? (
            <p>{content}</p>
          ) : (
            <div dangerouslySetInnerHTML={{ __html: `<p>${html}</p>` }} />
          )}
        </div>

        {/* Tool badges + cached indicator */}
        {(tools_used?.length > 0 || cached) && (
          <div className="message-meta">
            {tools_used?.map((t) => (
              <span key={t} className="tool-badge">
                {TOOL_LABELS[t] || t}
              </span>
            ))}
            {cached && <span className="cached-badge">⚡ Cached</span>}
          </div>
        )}

        {!isUser && <CopyButton text={content} />}
      </div>
    </div>
  );
}
