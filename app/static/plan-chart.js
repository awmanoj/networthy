// Lifetime projection chart: a shaded band between the pessimistic and
// optimistic return assumptions, with the base case drawn through it.
//
// Deliberately separate from chart.js rather than an option on it: that chart
// plots one dated series, this one plots three age-indexed series plus event
// markers, and folding them together would make both harder to read. It reuses
// the same CSS tokens so the two charts still look like one system.

function drawPlanChart(plan) {
  const host = document.getElementById("plan-chart");
  if (!host || !plan || !plan.base || plan.base.length === 0) return;

  const W = host.clientWidth || 720;
  const H = 300;
  const pad = { top: 18, right: 18, bottom: 40, left: 68 };
  const iw = W - pad.left - pad.right;
  const ih = H - pad.top - pad.bottom;
  const n = plan.base.length;

  const hi = Math.max(...plan.high, 1);
  const x = (i) => pad.left + (n === 1 ? iw / 2 : (i / (n - 1)) * iw);
  const y = (v) => pad.top + ih - (v / hi) * ih;

  const pts = (arr) => arr.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  // The band is the optimistic line out and the pessimistic line back.
  const bandPts =
    plan.high.map((v, i) => `${x(i)},${y(v)}`).join(" ") +
    " " +
    plan.low.map((v, i) => `${x(i)},${y(v)}`).reverse().join(" ");

  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e7) return "₹" + (v / 1e7).toFixed(a >= 1e8 ? 0 : 1) + " Cr";
    if (a >= 1e5) return "₹" + (v / 1e5).toFixed(0) + " L";
    return "₹" + Math.round(v).toLocaleString("en-IN");
  };

  // X labels every ~10 years, so ages stay readable at any width.
  const step = Math.max(1, Math.round(n / 7));
  const xlabels = plan.labels
    .map((age, i) =>
      i % step === 0 || i === n - 1
        ? `<text x="${x(i)}" y="${H - 14}" class="c-xlabel">${age}</text>`
        : ""
    )
    .join("");

  const yticks = [0, hi / 2, hi]
    .map((v) => `<text x="${pad.left - 10}" y="${y(v) + 4}" class="c-ylabel">${compact(v)}</text>`)
    .join("");

  // Retirement is the point the curve changes character — mark it.
  const ri = plan.labels.indexOf(plan.retire_age);
  const retireMark =
    ri >= 0
      ? `<line class="p-mark-line" x1="${x(ri)}" x2="${x(ri)}" y1="${pad.top}" y2="${pad.top + ih}" />
         <text class="p-mark-text" x="${x(ri) + 5}" y="${pad.top + 12}">retires at ${plan.retire_age}</text>`
      : "";

  // A dot on the year each one-off goal lands, on the base line.
  const events = (plan.events || [])
    .map((e) => {
      const i = plan.labels.indexOf(e.age);
      if (i < 0) return "";
      const title = e.labels.join(", ") + " · " + compact(e.amount);
      return `<circle class="p-event" cx="${x(i)}" cy="${y(plan.base[i])}" r="5"><title>${title}</title></circle>`;
    })
    .join("");

  host.innerHTML = `
    <div class="chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
           aria-label="Projected net worth by age, showing a range of return assumptions">
        <polygon points="${bandPts}" class="p-band" />
        <polyline points="${pts(plan.base)}" class="c-line" />
        ${retireMark}
        ${events}
        ${xlabels}
        ${yticks}
      </svg>
    </div>`;
}
