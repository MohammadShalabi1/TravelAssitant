import { createContext, useContext, useState, useEffect } from "react";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const token = localStorage.getItem("token");
    const email = localStorage.getItem("email");
    if (token && email) setUser({ token, email });
    setLoading(false);
  }, []);

  function loginSuccess(data) {
    localStorage.setItem("token", data.access_token);
    localStorage.setItem("email", data.email);
    setUser({ token: data.access_token, email: data.email });
  }

  function logout() {
    localStorage.removeItem("token");
    localStorage.removeItem("email");
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, loading, loginSuccess, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);
