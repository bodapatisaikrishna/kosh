"use client";

export default function DefectConfusion({ defectConfusion }) {
  const entries = Object.entries(defectConfusion || {});
  if (entries.length === 0) return null;

  return (
    <section>
      <h2>Per-defect-class confusion</h2>
      <table>
        <thead>
          <tr>
            <th>Defect type</th>
            <th>Outcome counts</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([dtype, counts]) => (
            <tr key={dtype}>
              <td>{dtype}</td>
              <td>{JSON.stringify(counts)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
