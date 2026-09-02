// Mirrors eval/report.py's _pct/_rupees exactly, so a number reads the same
// whether it came from the static HTML report or this dashboard.

export function pct(x) {
  return `${(x * 100).toFixed(2)}%`;
}

export function rupees(paise) {
  const sign = paise < 0 ? "-" : "";
  const value = (Math.abs(paise) / 100).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${sign}₹${value}`;
}
