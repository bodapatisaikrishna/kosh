"use client";

import { useState } from "react";

import { getBenchmark, runLive } from "@/lib/api";
import BenchmarkPicker from "@/components/BenchmarkPicker";
import CashPosition from "@/components/CashPosition";
import DefectConfusion from "@/components/DefectConfusion";
import ExceptionQueue from "@/components/ExceptionQueue";
import HeadlineCards from "@/components/HeadlineCards";
import LayerWaterfall from "@/components/LayerWaterfall";
import RunForm from "@/components/RunForm";

export default function Home() {
  const [mode, setMode] = useState("live");
  const [report, setReport] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [elapsed, setElapsed] = useState(null);

  async function handleRunLive(params) {
    setRunning(true);
    setError(null);
    const started = performance.now();
    try {
      const result = await runLive(params);
      setReport(result);
      setElapsed(((performance.now() - started) / 1000).toFixed(2));
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  }

  async function handleSelectBenchmark(name) {
    setRunning(true);
    setError(null);
    try {
      const result = await getBenchmark(name);
      setReport(result);
      setElapsed(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <main className="page">
      <h1>Kosh reconciliation - live dashboard</h1>
      <p className="subtitle">
        The brief&apos;s own optional stretch goal, built after the code freeze - see{" "}
        <code>RESULTS.md</code>&apos;s &quot;Post-submission stretch goals&quot;. The static HTML report
        (<code>make demo</code>) remains the primary deliverable; this is additive.
      </p>

      <div className="mode-toggle">
        <button className={mode === "live" ? "active" : ""} onClick={() => setMode("live")}>
          Live run
        </button>
        <button className={mode === "historical" ? "active" : ""} onClick={() => setMode("historical")}>
          Historical benchmarks
        </button>
      </div>

      {mode === "live" ? (
        <RunForm onRun={handleRunLive} running={running} />
      ) : (
        <BenchmarkPicker onSelect={handleSelectBenchmark} loading={running} />
      )}

      {error && (
        <p className="error-banner">
          {error}. Is the API running (<code>make api</code>)?
        </p>
      )}

      {elapsed && !running && (
        <p className="muted">Live run completed in {elapsed}s, timer starting the moment the request was sent.</p>
      )}

      {report && (
        <>
          <HeadlineCards metrics={report.metrics} cash={report.cash} />
          <section>
            <h2>Layer waterfall</h2>
            <p className="muted">Share of correctly-matched links contributed by each layer.</p>
            <LayerWaterfall layerContribution={report.metrics.accuracy.layer_contribution} />
          </section>
          <ExceptionQueue
            exceptions={report.exceptions_detail}
            totalAtRiskPaise={report.metrics.exceptions.total_amount_at_risk_paise}
          />
          <CashPosition cash={report.cash} />
          <DefectConfusion defectConfusion={report.metrics.accuracy.defect_confusion} />
        </>
      )}
    </main>
  );
}
