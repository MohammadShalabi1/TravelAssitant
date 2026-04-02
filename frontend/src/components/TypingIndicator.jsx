export default function TypingIndicator() {
  return (
    <div className="message assistant">
      <div className="avatar"><span>✈</span></div>
      <div className="message-body">
        <div className="bubble typing-bubble">
          <span className="dot" />
          <span className="dot" />
          <span className="dot" />
        </div>
      </div>
    </div>
  );
}
