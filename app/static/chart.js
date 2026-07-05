// Minimal dependency-free SVG line chart for the net-worth timeline.
// Kept local (no CDN) so financial data never triggers an external request.

function drawChart(data) {
  const host = document.getElementById("chart");
  if (!host) return;
  if (!data || data.length === 0) {
    host.innerHTML = '<p class="muted">Not enough data to chart yet.</p>';
    return;
  }

  const W = host.clientWidth || 720;
  const H = 280;
  const pad = { top: 20, right: 20, bottom: 36, left: 64 };
  const iw = W - pad.left - pad.right;
  const ih = H - pad.top - pad.bottom;

  const values = data.map((d) => d.value);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const span = maxV - minV || maxV || 1;
  // Pad the value axis so the line isn't glued to the edges.
  const lo = minV - span * 0.1;
  const hi = maxV + span * 0.1;

  const x = (i) =>
    pad.left + (data.length === 1 ? iw / 2 : (i / (data.length - 1)) * iw);
  const y = (v) => pad.top + ih - ((v - lo) / (hi - lo)) * ih;

  const line = data.map((d, i) => `${x(i)},${y(d.value)}`).join(" ");
  const area = `${pad.left},${pad.top + ih} ${line} ${pad.left + iw},${pad.top + ih}`;

  const fmt = (v) =>
    "₹" + Math.round(v).toLocaleString("en-IN");

  const dots = data
    .map(
      (d, i) =>
        `<circle cx="${x(i)}" cy="${y(d.value)}" r="3.5" class="c-dot">` +
        `<title>${d.date} · ${fmt(d.value)}</title></circle>`
    )
    .join("");

  const labels = data
    .map((d, i) => {
      // Thin out x labels when crowded.
      const step = Math.ceil(data.length / 8);
      if (i % step !== 0 && i !== data.length - 1) return "";
      return `<text x="${x(i)}" y="${H - 12}" class="c-xlabel">${d.date.slice(
        0,
        7
      )}</text>`;
    })
    .join("");

  const yticks = [lo, (lo + hi) / 2, hi]
    .map(
      (v) =>
        `<text x="${pad.left - 10}" y="${y(v) + 4}" class="c-ylabel">${fmt(
          v
        )}</text>`
    )
    .join("");

  host.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
         aria-label="Net worth over time">
      <polyline points="${area}" class="c-area" />
      <polyline points="${line}" class="c-line" />
      ${dots}
      ${labels}
      ${yticks}
    </svg>`;
}
