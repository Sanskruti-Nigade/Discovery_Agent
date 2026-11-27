import { useCallback, useEffect, useState } from "react";
import { health, reviewSummary, systemHealth } from "./api.js";
import { Link, useLocation } from "./router.jsx";
import ChatPage from "./pages/ChatPage.jsx";
import GraphPage from "./pages/GraphPage.jsx";
import ReviewsPage from "./pages/ReviewsPage.jsx";

const APP_NAME = import.meta.env.VITE_APP_NAME || "Discovery Agent";
const LOGO_URL = import.meta.env.VITE_LOGO_URL || "/servicenow-logo.svg";

function Brand() {
  const [logoOk, setLogoOk] = useState(true);
  return (
    <Link to="/" className="brand">
      {logoOk
        ? <img src={LOGO_URL} alt="ServiceNow" onError={() => setLogoOk(false)} />
        : <span className="logo-text">ServiceNow</span>}
      <span className="divider" />
      <span className="app-name">{APP_NAME}</span>
    </Link>
  );
}

export default function App() {
  const { path, search } = useLocation();
  const [neo4j, setNeo4j] = useState(null);
  const [summary, setSummary] = useState(null);
  const [status, setStatus] = useState(null);

  const refreshSummary = useCallback(() => {
    reviewSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  useEffect(() => {
    document.title = APP_NAME;
    health().then(setNeo4j).catch(() => setNeo4j({ neo4j: false }));  // graph counts for the chat page
    const poll = () => {
      refreshSummary();
      systemHealth().then(setStatus).catch(() => setStatus(null));
    };
    poll();
    const t = setInterval(poll, 30000);
    return () => clearInterval(t);
  }, [refreshSummary]);

  const page = path.startsWith("/reviews") ? "reviews" : path.startsWith("/graph") ? "graph" : "chat";
  const pending = summary?.pending_batches || 0;

  return (
    <div className="shell">
      <header className="topbar">
        <Brand />
        <nav className="nav">
          <Link to="/" className={page === "chat" ? "active" : ""}>Ask</Link>
          <Link to="/graph" className={page === "graph" ? "active" : ""}>Lineage graph</Link>
          <Link to="/reviews" className={page === "reviews" ? "active" : ""}>
            Reviews {pending > 0 && <span className="badge" title={`${pending} batch(es) waiting for approval`}>{pending}</span>}
          </Link>
        </nav>
        <div className="spacer" />
        <div className="conn">
          {[["neo4j", "Neo4j"], ["postgres", "PostgreSQL"], ["llm", "LLM"]].map(([key, label]) => {
            const c = status?.checks?.[key];
            const tip = !c ? "checking…" : c.ok
              ? (key === "llm" ? `${c.provider} · ${c.model || ""}${c.verified ? " · key verified" : ""}` : "connected")
              : c.error;
            return (
              <span key={key} title={tip}>
                <i className={`dot ${!c ? "" : c.ok ? "on" : "off"}`} />{key === "llm" && c?.provider ? `LLM (${c.provider})` : label}
              </span>
            );
          })}
        </div>
      </header>
      <div className="page">
        {page === "chat" && <ChatPage health={neo4j} />}
        {page === "graph" && <GraphPage />}
        {page === "reviews" && <ReviewsPage search={search} summary={summary} onChanged={refreshSummary} />}
      </div>
    </div>
  );
}
