import { useEffect, useMemo, useState } from "react";
import { Record, Stats, Toolbar } from "../components/PageBits";
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

  const typeOptions = useMemo(
    () => TYPES.map((value) => ({ id: value, label: value || "All types" })),
    [],
  );

  return (
    <>
      <h1>Memory</h1>
      <p className="subtle">
        Everything Dobot remembers is listed here and can be deleted. Nothing is injected into a prompt
        without appearing in the recall view below.
      </p>

      <Stats
        items={[
          { label: "Memories", value: stats?.total ?? 0 },
          { label: "Store", value: stats?.store ?? "—", hint: "where records live" },
          { label: "Vectors", value: stats?.vectors ?? "—", hint: "retrieval index" },
          { label: "Embedder", value: stats?.embedder ?? "—" },
        ]}
      />

      <div className="record" style={{ marginTop: 14 }}>
        <div className="record__head">
          <span className="record__title">Remember something</span>
        </div>
        <div className="row">
          <input
            placeholder="e.g. The user prefers Python for backend work"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            style={{ maxWidth: 520 }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && draft.trim()) void remember();
            }}
          />
          <button className="primary" disabled={!draft.trim()} onClick={() => void remember()}>
            Remember
          </button>
        </div>
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
                <td className="mono">
                  {Object.entries(hit.components)
                    .map(([key, value]) => `${key}=${value.toFixed(2)}`)
                    .join(" ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Stored memories</h2>
      <Toolbar
        query={search}
        onQuery={setSearch}
        placeholder="Search what is stored…"
        options={typeOptions}
        active={type}
        onActive={setType}
        count={
          search || type
            ? `${memories.length} match${memories.length === 1 ? "" : "es"}`
            : `${memories.length} stored`
        }
      />

      {memories.length === 0 && <div className="notice">Nothing remembered yet.</div>}

      <div className="record-list">
        {memories.map((memory) => (
          <Record
            key={memory.id}
            title={memory.content}
            meta={
              <>
                <span>{memory.type}</span>
                <span>importance {memory.importance.toFixed(2)}</span>
                <span>{new Date(memory.created_at).toLocaleString()}</span>
              </>
            }
            actions={
              <button
                className="ghost"
                onClick={() => void api.forget(memory.id).then(load)}
              >
                Forget
              </button>
            }
          />
        ))}
      </div>
    </>
  );

  async function remember() {
    await api.remember({ content: draft.trim(), importance: 0.7 });
    setDraft("");
    await load();
  }
}
