"use client";

import { useEffect, useState } from "react";

import { listBenchmarks } from "@/lib/api";

const LABELS = {
  freeze_500: "Frozen benchmark - 500 records",
  freeze_2000: "Frozen benchmark - 2,000 records",
  freeze_10000: "Frozen benchmark - 10,000 records",
  phase3: "Phase 3 checkpoint (L0+L1)",
  phase4: "Phase 4 checkpoint (L0+L1+L2)",
  phase5: "Phase 5 checkpoint (full pipeline)",
  ablation_llm_only: "All-LLM ablation (sample_200)",
};

export default function BenchmarkPicker({ onSelect, loading }) {
  const [names, setNames] = useState([]);
  const [error, setError] = useState(null);

  useEffect(() => {
    listBenchmarks()
      .then((data) => setNames(data.benchmarks))
      .catch((err) => setError(err.message));
  }, []);

  if (error) {
    return <p className="muted">Couldn&apos;t reach the API: {error}. Is `make api` running?</p>;
  }

  return (
    <div className="run-form">
      <label>
        Historical benchmark
        <select disabled={loading} defaultValue="" onChange={(e) => e.target.value && onSelect(e.target.value)}>
          <option value="" disabled>
            {names.length === 0 ? "loading…" : "choose one"}
          </option>
          {names.map((name) => (
            <option key={name} value={name}>
              {LABELS[name] || name}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
