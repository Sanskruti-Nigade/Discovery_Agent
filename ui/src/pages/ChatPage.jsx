import { useEffect, useRef, useState } from "react";
import { ask } from "../api.js";

// Ported from frontend/static/index.html (graph dev's lineage Q&A); same /api/ask contract.
const SUGGESTIONS = [
  "Where does dim_model.schema_version come from?",
  "What does model_product.id feed?",
  "columns of dim_product",
  "What is impacted if I change model_product.id?",
  "show latest changes and previous",
];

function Table({ head, rows, bold }) {
  return (
    <div className="tbl">
      <table>
        <thead><tr>{head.map((h) => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} style={bold?.(i) ? { fontWeight: 700 } : undefined}>
              {r.map((v, j) => <td key={j}>{v ?? ""}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BotAnswer({ r, onClarify, clarified }) {
  return (
    <>
      {r.answer}
      {r.clarify && (
        <div className="clarify">
          {r.clarify.options.map((o) => (
            <button key={o.name} type="button" className="btn sm" disabled={clarified} onClick={() => onClarify(o.name)}>
              {o.name} <span className="muted">({o.kind})</span>
            </button>
          ))}
        </div>
      )}
      {r.history?.length > 0 && (
        <Table
          head={["Order", "When", "Change", "System", "Table", "Column", "Previous", "Latest"]}
          rows={r.history.map((m) => [m.order, m.when, m.what, m.system, m.table, m.column, m.previous, m.latest])}
          bold={(i) => r.history[i].order === "Latest"}
        />
      )}
      {r.impact?.length > 0 && (
        <Table
          head={["Changed column", "Impacted system", "Impacted table", "Impacted column", "Type", "Hops", "Path"]}
          rows={r.impact.map((m) => [m.changed, m.system, m.table, m.column, m.data_type, m.level, m.path])}
        />
      )}
      {r.matches?.length > 0 && (
        <details>
          <summary>{r.matches.length} matching column(s) in Neo4j</summary>
          <Table
            head={["Column", "Type", "From", "Feeds"]}
            rows={r.matches.map((m) => [`${m.system}.${m.table}.${m.name}`, m.data_type || "", m.upstream.length, m.downstream.length])}
          />
        </details>
      )}
    </>
  );
}

export default function ChatPage({ health }) {
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const scroller = useRef(null);
  const input = useRef(null);

  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" }); }, [messages]);

  async function send(question) {
    const q = question.trim();
    if (!q || busy) return;
    setText("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text: q }, { role: "bot", pending: true }]);
    let reply;
    try {
      reply = await ask(q);
    } catch (e) {
      reply = { answer: `Error: ${e.message}`, error: true };
    }
    setMessages((m) => [...m.slice(0, -1), { role: "bot", r: reply, question: q }]);
    setBusy(false);
    input.current?.focus();
  }

  function clarify(index, name) {
    const msg = messages[index];
    setMessages((m) => m.map((x, i) => (i === index ? { ...x, clarified: true } : x)));
    send(msg.question.split(msg.r.clarify.term).join(name));
  }

  return (
    <div className="chat">
      <div className="chat-scroll" ref={scroller}>
        <div className="chat-inner">
          {messages.length === 0 && (
            <div className="welcome">
              <h1>Ask about your data lineage</h1>
              <p>
                Where a column comes from, what it feeds, what breaks if it changes, and what changed recently.
                {health && (
                  <span className="small"><br />
                    {health.neo4j
                      ? `Graph: ${health.counts.map((c) => `${c.n} ${c.label}`).join(", ")}`
                      : `Neo4j not reachable${health.error ? `: ${health.error}` : ""}`}
                  </span>
                )}
              </p>
              <div className="suggestions">
                {SUGGESTIONS.map((s) => <button key={s} className="suggestion" onClick={() => send(s)}>{s}</button>)}
              </div>
            </div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <div className={`bubble ${m.r?.error ? "error" : ""}`}>
                {m.role === "user" && m.text}
                {m.pending && <span className="typing"><i /><i /><i /></span>}
                {m.r && <BotAnswer r={m.r} clarified={m.clarified} onClarify={(name) => clarify(i, name)} />}
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="composer">
        <form onSubmit={(e) => { e.preventDefault(); send(text); }}>
          <input ref={input} type="text" value={text} onChange={(e) => setText(e.target.value)}
                 placeholder="Ask a question about your data lineage..." autoComplete="off" autoFocus />
          <button className="btn primary" disabled={busy || !text.trim()}>Ask</button>
        </form>
      </div>
    </div>
  );
}
