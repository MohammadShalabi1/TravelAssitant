import { createContext, useContext, useEffect, useState } from "react";
import { logoutRequest, refreshAuth, setAccessToken } from "./api";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function restoreSession() {
      try {
        const data = await refreshAuth();
        if (!cancelled && data?.access_token) {
          setUser({ email: data.email, user_id: data.user_id });
        }
      } catch (_err) {
        setAccessToken(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    restoreSession();
    return () => {
      cancelled = true;
    };
  }, []);

  function loginSuccess(data) {
    setAccessToken(data.access_token);
    setUser({ email: data.email, user_id: data.user_id });
  }

  async function logout() {
    await logoutRequest();
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, loading, loginSuccess, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
