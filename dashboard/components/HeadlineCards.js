"use client";

import { pct, rupees } from "@/lib/format";

// Mirrors eval/report.py:render_html's headline strip - same cards, same
// numbers, same "only show an LLM cost card when llm_calls > 0" rule (a
// deterministic-only run never shows a misleading "$0.00", which would read
// as "free" rather than "L3 didn't run").
export default function HeadlineCards({ metrics, cash }) {
  const { accuracy, throughput } = metrics;

  const cards = [
    { label: "Records processed", value: throughput.records_processed },
    { label: "Auto-match rate", value: pct(accuracy.auto_match_rate) },
    { label: "False-match rate", value: pct(accuracy.false_match_rate), risk: true },
    { label: "Rs reconciled", value: rupees(cash.reconciled_cash_paise) },
    { label: "Wall clock", value: `${throughput.wall_clock_seconds.toFixed(3)}s` },
  ];

  if (throughput.llm_calls > 0) {
    cards.push({
      label: "LLM cost (L3)",
      value: `$${(throughput.cost_usd_micros / 1_000_000).toFixed(4)}`,
    });
  }

  return (
    <div className="headline">
      {cards.map((card) => (
        <div className={`card${card.risk ? " risk" : ""}`} key={card.label}>
          <div className="label">{card.label}</div>
          <div className="value">{card.value}</div>
        </div>
      ))}
    </div>
  );
}
