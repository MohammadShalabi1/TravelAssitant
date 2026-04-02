import { AuthProvider, useAuth } from "./lib/AuthContext";
import AuthPage from "./pages/AuthPage";
import ChatPage from "./pages/ChatPage";

function AppInner() {
  const { user, loading } = useAuth();
  if (loading) return <div className="app-loading"><span className="logo-icon spin">✈</span></div>;
  return user ? <ChatPage /> : <AuthPage />;
}

export default function App() {
  return (
    <AuthProvider>
      <AppInner />
    </AuthProvider>
  );
}
