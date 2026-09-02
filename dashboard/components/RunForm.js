"use client";

import { useState } from "react";

// The API only ever accepts these 5 engines - see api/runs.py's
// ENGINE_ALLOWLIST. "llm-only" is never offered here; that safety boundary is
// enforced server-side too, this is just not presenting an option the API
// would reject anyway.
const ENGINES = [
  { value: "null", label: "Null (matches nothing)" },
  { value: "oracle", label: "Oracle (reads ground truth)" },
  { value: "l0l1", label: "L0 + L1" },
  { value: "l0l1l2", label: "L0 + L1 + L2" },
  { value: "full", label: "Full pipeline" },
];

const RECORD_PRESETS = [500, 2000, 10000];

export default function RunForm({ onRun, running }) {
  const [engine, setEngine] = useState("full");
  const [records, setRecords] = useState(2000);
  const [seed, setSeed] = useState(42);
  const [months, setMonths] = useState(3);

  function handleSubmit(e) {
    e.preventDefault();
    onRun({ engine, records: Number(records), seed: Number(seed), months: Number(months) });
  }

  return (
    <form className="run-form" onSubmit={handleSubmit}>
      <label>
        Engine
        <select value={engine} onChange={(e) => setEngine(e.target.value)}>
          {ENGINES.map((e) => (
            <option key={e.value} value={e.value}>
              {e.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Records
        <select value={records} onChange={(e) => setRecords(e.target.value)}>
          {RECORD_PRESETS.map((n) => (
            <option key={n} value={n}>
              {n.toLocaleString("en-IN")}
            </option>
          ))}
        </select>
      </label>
      <label>
        Seed
        <input
          type="number"
          value={seed}
          onChange={(e) => setSeed(e.target.value)}
          title="Try 100 or 31337 - the multiseed sweep's own seeds (RESULTS.md SS11.1) - to watch false-match stay 0.00% on a fresh dataset."
        />
      </label>
      <label>
        Months
        <input type="number" min={1} max={12} value={months} onChange={(e) => setMonths(e.target.value)} />
      </label>
      <button type="submit" disabled={running}>
        {running ? "Running…" : "Run live"}
      </button>
    </form>
  );
}
