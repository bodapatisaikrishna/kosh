"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { rupees } from "@/lib/format";

// Mirrors eval/report.py:render_html's cash panel: the 14-day inflow curve as
// a bar chart, then the book-vs-reconciled reconciliation table where every
// paisa of the gap is named (cash/forecast.py's own invariant - see its
// module docstring: "the gap must be fully explained by named components, to
// the paisa").
export default function CashPosition({ cash }) {
  const chartData = cash.inflow_curve.map((row) => ({
    date: row.date.slice(5),
    inflowRupees: Math.round(row.expected_inflow_paise / 100),
  }));

  return (
    <section>
      <h2>Cash position</h2>
      <p className="muted">
        As of {cash.as_of_date} &middot; Rs stuck: <strong>{rupees(cash.stuck_paise)}</strong> (
        {cash.stuck_payment_ids.length} payments) &middot; Rs at risk: <strong>{rupees(cash.at_risk_paise)}</strong>
      </p>

      <h3 style={{ fontSize: "0.95rem", marginBottom: "0.3rem" }}>14-day expected inflow</h3>
      <div style={{ width: "100%", maxWidth: 720, height: 160 }}>
        <ResponsiveContainer>
          <BarChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip formatter={(value) => [`Rs ${value.toLocaleString("en-IN")}`, "expected inflow"]} />
            <Bar dataKey="inflowRupees" fill="#2b6cb0" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      <h3 style={{ fontSize: "0.95rem", margin: "1.2rem 0 0.3rem" }}>Book cash vs. reconciled cash</h3>
      <p className="muted">
        Book: {rupees(cash.book_cash_paise)} &middot; Reconciled: {rupees(cash.reconciled_cash_paise)} &middot; every
        paisa of the gap is named below
      </p>
      <table>
        <thead>
          <tr>
            <th>Component</th>
            <th className="num">Amount</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(cash.reconciliation).map(([name, value]) => (
            <tr key={name}>
              <td>{name}</td>
              <td className="num">{rupees(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
