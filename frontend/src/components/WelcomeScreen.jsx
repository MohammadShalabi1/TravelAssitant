const SUGGESTIONS = [
  { icon: "🌤", text: "What's the weather in Tokyo right now?" },
  { icon: "🍜", text: "Find restaurants near the Eiffel Tower" },
  { icon: "🏖", text: "Best beaches in Bali for surfing" },
  { icon: "🗺", text: "Cafés near the Louvre Museum" },
];

export default function WelcomeScreen({ onSuggestion }) {
  return (
    <div className="welcome-screen">
      <div className="welcome-icon">✈</div>
      <h2 className="welcome-title">Where to next?</h2>
      <p className="welcome-sub">
        Ask me about weather, nearby places, or travel tips for any destination.
      </p>
      <div className="suggestions">
        {SUGGESTIONS.map((s) => (
          <button key={s.text} className="suggestion-chip" onClick={() => onSuggestion(s.text)}>
            <span>{s.icon}</span> {s.text}
          </button>
        ))}
      </div>
    </div>
  );
}
