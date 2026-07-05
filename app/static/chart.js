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

  const fmt = (v) => "₹" + Math.round(v).toLocaleString("en-IN");
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const fmtDate = (iso) => {
    const [Y, M, D] = iso.split("-");
    return `${D} ${MONTHS[parseInt(M, 10) - 1]} ${Y}`;
  };

  const dots = data
    .map((d, i) => `<circle cx="${x(i)}" cy="${y(d.value)}" r="3" class="c-dot" />`)
    .join("");

  const labels = data
    .map((d, i) => {
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
    <div class="chart-wrap">
      <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" role="img"
           aria-label="Net worth over time">
        <polyline points="${area}" class="c-area" />
        <polyline points="${line}" class="c-line" />
        <line class="c-guide" y1="${pad.top}" y2="${pad.top + ih}" style="display:none" />
        ${dots}
        <circle class="c-focus" r="5" style="display:none" />
        ${labels}
        ${yticks}
        <rect class="c-hit" x="${pad.left}" y="${pad.top}"
              width="${iw}" height="${ih}" fill="transparent" />
      </svg>
      <div class="c-tip" style="display:none"></div>
    </div>`;

  // --- Hover interaction: snap to the nearest point, show a readable tooltip.
  const svg = host.querySelector("svg");
  const guide = host.querySelector(".c-guide");
  const focus = host.querySelector(".c-focus");
  const tip = host.querySelector(".c-tip");
  const hit = host.querySelector(".c-hit");

  function moveTo(clientX) {
    const rect = svg.getBoundingClientRect();
    const scaleX = rect.width / W;
    const scaleY = rect.height / H;
    const mx = (clientX - rect.left) / scaleX; // cursor x in viewBox units

    // Nearest data point by x (points are evenly spaced, but iterate to be safe).
    let i = 0;
    let best = Infinity;
    for (let k = 0; k < data.length; k++) {
      const dx = Math.abs(x(k) - mx);
      if (dx < best) {
        best = dx;
        i = k;
      }
    }

    const px = x(i);
    const py = y(data[i].value);

    guide.setAttribute("x1", px);
    guide.setAttribute("x2", px);
    guide.style.display = "";
    focus.setAttribute("cx", px);
    focus.setAttribute("cy", py);
    focus.style.display = "";

    tip.innerHTML =
      `<span class="c-tip-date">${fmtDate(data[i].date)}</span>` +
      `<span class="c-tip-val">${fmt(data[i].value)}</span>`;
    tip.style.display = "";
    tip.style.left = px * scaleX + "px";
    tip.style.top = py * scaleY + "px";
  }

  function hide() {
    guide.style.display = "none";
    focus.style.display = "none";
    tip.style.display = "none";
  }

  hit.addEventListener("mousemove", (e) => moveTo(e.clientX));
  hit.addEventListener("mouseleave", hide);
  hit.addEventListener(
    "touchstart",
    (e) => e.touches[0] && moveTo(e.touches[0].clientX),
    { passive: true }
  );
  hit.addEventListener(
    "touchmove",
    (e) => e.touches[0] && moveTo(e.touches[0].clientX),
    { passive: true }
  );
}
