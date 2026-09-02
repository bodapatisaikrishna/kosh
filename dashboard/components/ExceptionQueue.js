"use client";

import { useMemo, useState } from "react";

import { traceUrl } from "@/lib/api";
import { rupees } from "@/lib/format";

// Mirrors eval/report.py:render_html's exception queue exactly: click a row
// to expand evidence, sort by amount or category independently (the
// dashboard's own historical bug: a "Category" header that silently sorted
// by amount regardless - see ARCHITECTURE.md - so this keeps the two sorts
// deliberately separate rather than one shared toggle).
export default function ExceptionQueue({ exceptions, totalAtRiskPaise }) {
  const [sortBy, setSortBy] = useState("amount");
  const [ascending, setAscending] = useState(false);
  const [expandedIndex, setExpandedIndex] = useState(null);

  const sorted = useMemo(() => {
    const copy = [...exceptions];
    copy.sort((a, b) => {
      const diff =
        sortBy === "amount"
          ? a.amount_at_risk_paise - b.amount_at_risk_paise
          : a.category.localeCompare(b.category);
      return ascending ? diff : -diff;
    });
    return copy;
  }, [exceptions, sortBy, ascending]);

  function toggleSort(column) {
    if (sortBy === column) {
      setAscending(!ascending);
    } else {
      setSortBy(column);
      setAscending(false);
    }
  }

  return (
    <section>
      <h2>Exception queue</h2>
      <p>
        {exceptions.length} exceptions &middot; {rupees(totalAtRiskPaise)} at risk &middot;{" "}
        <span className="muted">click a row for evidence + trace</span>
      </p>
      <table>
        <thead>
          <tr>
            <th className="sortable" onClick={() => toggleSort("category")}>
              Category
            </th>
            <th>Severity</th>
            <th className="sortable num" onClick={() => toggleSort("amount")}>
              Rs at risk
            </th>
            <th>Owner</th>
            <th>Aging</th>
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 && (
            <tr>
              <td colSpan={5}>no exceptions</td>
            </tr>
          )}
          {sorted.map((exception, i) => (
            <ExceptionRow
              key={`${exception.category}-${i}`}
              exception={exception}
              expanded={expandedIndex === i}
              onToggle={() => setExpandedIndex(expandedIndex === i ? null : i)}
            />
          ))}
        </tbody>
      </table>
    </section>
  );
}

function ExceptionRow({ exception, expanded, onToggle }) {
  return (
    <>
      <tr className="exc-row" onClick={onToggle}>
        <td>{exception.category}</td>
        <td>
          <span className={`pill pill-${exception.severity.toLowerCase()}`}>{exception.severity}</span>
        </td>
        <td className="num">{rupees(exception.amount_at_risk_paise)}</td>
        <td>{exception.suggested_owner}</td>
        <td>{exception.aging_days}d</td>
      </tr>
      {expanded && (
        <tr className="exc-detail">
          <td colSpan={5}>
            <div className="detail-box">
              <strong>Affected:</strong> {JSON.stringify(exception.affected)}
              <br />
              <strong>Recommended action:</strong> {exception.recommended_action}
              <br />
              <strong>Evidence chain:</strong>
              <ul>
                {(exception.evidence_chain || []).length === 0 ? (
                  <li>
                    <em>no evidence recorded</em>
                  </li>
                ) : (
                  exception.evidence_chain.map((line, i) => <li key={i}>{line}</li>)
                )}
              </ul>
              {exception.trace_file ? (
                <a href={traceUrl(exception.trace_file)} target="_blank" rel="noreferrer">
                  view agent trace &rarr;
                </a>
              ) : (
                <span className="muted">no agent trace (deterministically classified)</span>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
