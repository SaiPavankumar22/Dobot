import { useEffect, useState } from "react";
import { api } from "../services/api";
import type { MemoryHit, MemoryRecord } from "../types";

const TYPES = ["", "preference", "project", "person", "fact", "workflow", "episode", "task"];

export function Memory() {
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [type, setType] = useState("");
  const [search, setSearch] = useState("");
  const [stats, setStats] = useState<{ total: number; store: string; vectors: string; embedder: string } | null>(null);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<MemoryHit[]>([]);
  const [draft, setDraft] = useState("");

  async function load() {
    setMemories(await api.memories({ type: type || undefined, search: search || undefined }));
    setStats(await api.memoryStats());
  }

  useEffect(() => {
    void load();
  }, [type, search]);

  return (
    <>
      <h1>Memory</h1>
      <p className="subtle">
        Everything Dobot remembers is listed here and can be deleted. Nothing is injected into a prompt
        without appearing in the recall view below.
      </p>

      <div className="cards">
        <div className="card">
          <div className="card__label">Total</div>
          <div className="card__value">{stats?.total ?? 0}</div>
        </div>
        <div className="card">
          <div className="card__label">Store</div>
          <div className="card__value" style={{ fontSize: 16 }}>{stats?.store ?? "—"}</div>
        </div>
        <div className="card">
          <div className="card__label">Vectors</div>
          <div className="card__value" style={{ fontSize: 16 }}>{stats?.vectors ?? "—"}</div>
        </div>
        <div className="card">
          <div className="card__label">Embedder</div>
          <div className="card__value" style={{ fontSize: 13 }}>{stats?.embedder ?? "—"}</div>
        </div>
      </div>

      <h2>Add a memory</h2>
      <div className="row">
        <input
          placeholder="e.g. The user prefers Python for backend work"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          style={{ maxWidth: 520 }}
        />
        <button
          className="primary"
          disabled={!draft.trim()}
          onClick={async () => {
            await api.remember({ content: draft.trim(), importance: 0.7 });
            setDraft("");
            await load();
          }}
        >
          Remember
        </button>
      </div>

      <h2>Retrieval audit</h2>
      <p className="subtle">What would be recalled for a query, and why (similarity / recency / importance / task).</p>
      <div className="row">
        <input
          placeholder="Query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          style={{ maxWidth: 420 }}
        />
        <button onClick={async () => setHits(await api.recall(query))} disabled={!query.trim()}>
          Recall
        </button>
      </div>
      {hits.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Memory</th>
              <th>Score</th>
              <th>Components</th>
            </tr>
          </thead>
          <tbody>
            {hits.map((hit) => (
              <tr key={hit.memory.id}>
                <td>{hit.memory.content}</td>
                <td className="mono">{hit.score.toFixed(3)}</td>
                <td className="mono">{Object.entries(hit.components).map(([key, value]) => `${key}=${value.toFixed(2)}`).join(" ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Stored memories</h2>
      <div className="row">
        <select value={type} onChange={(event) => setType(event.target.value)} style={{ maxWidth: 160 }}>
          {TYPES.map((option) => (
            <option key={option} value={option}>
              {option || "all types"}
            </option>
          ))}
        </select>
        <input
          placeholder="Search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          style={{ maxWidth: 260 }}
        />
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>Content</th>
            <th>Type</th>
            <th>Importance</th>
            <th>Added</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {memories.map((memory) => (
            <tr key={memory.id}>
              <td>{memory.content}</td>
              <td className="mono">{memory.type}</td>
              <td className="mono">{memory.importance.toFixed(2)}</td>
              <td className="mono">{new Date(memory.created_at).toLocaleString()}</td>
              <td>
                <button className="ghost" onClick={() => void api.forget(memory.id).then(load)}>
                  Forget
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {memories.length === 0 && <div className="notice">Nothing remembered yet.</div>}
    </>
  );
}
