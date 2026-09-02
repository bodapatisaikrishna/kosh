import "./globals.css";

export const metadata = {
  title: "Kosh - live dashboard",
  description: "Kosh reconciliation - live run + historical benchmarks (post-freeze stretch goal).",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
