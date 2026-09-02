"use client";

import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

const LAYER_COLOR = { L0: "#2b6cb0", L1: "#38a169", L2: "#dd6b20", L3: "#805ad5" };

// Same data eval/report.py's static table shows (layer_contribution: share of
// correctly-matched links contributed by each layer) - a bar chart instead of
// a table, since this is the one panel a chart genuinely clarifies over text.
export default function LayerWaterfall({ layerContribution }) {
  const data = Object.entries(layerContribution).map(([layer, share]) => ({
    layer,
    sharePct: Number((share * 100).toFixed(2)),
  }));

  if (data.length === 0) {
    return <p className="muted">no matches</p>;
  }

  return (
    <div style={{ width: "100%", height: 220 }}>
      <ResponsiveContainer>
        <BarChart data={data} layout="vertical" margin={{ left: 8, right: 24 }}>
          <CartesianGrid strokeDasharray="3 3" horizontal={false} />
          <XAxis type="number" unit="%" domain={[0, 100]} />
          <YAxis type="category" dataKey="layer" width={40} />
          <Tooltip formatter={(value) => [`${value}%`, "share of correct matches"]} />
          <Bar dataKey="sharePct" radius={[0, 4, 4, 0]}>
            {data.map((entry) => (
              <Cell key={entry.layer} fill={LAYER_COLOR[entry.layer] || "#718096"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
