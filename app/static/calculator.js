// Net worth calculator — runs entirely in the browser.
//
// Same rule as standing.js and retire.js: a visitor types their assets and debts
// into a public page, so the only defensible design is that none of it leaves the
// tab. There is no fetch in this file and there must never be one.
//
// The arithmetic is deliberately the whole of the formula: sum one side, sum the
// other, subtract. Anything cleverer here would be a second opinion competing
// with app/networth.py's rollup, which is the real one.

function initCalculator() {
  const netEl = document.getElementById("calc-net");
  const assetEl = document.getElementById("calc-assets");
  const liabEl = document.getElementById("calc-liabilities");
  const noteEl = document.getElementById("calc-note");
  const nextEl = document.getElementById("calc-next");
  const rankEl = document.getElementById("calc-rank-link");
  const inputs = Array.from(document.querySelectorAll(".calc-in"));
  if (!netEl || !inputs.length) return;
  let lastNet = 0;

  const inr = (v) => "₹" + Math.round(v).toLocaleString("en-IN");
  const compact = (v) => {
    const a = Math.abs(v);
    const sign = v < 0 ? "−" : "";
    if (a >= 1e7) return sign + "₹" + (a / 1e7).toFixed(a >= 1e8 ? 1 : 2) + " crore";
    if (a >= 1e5) return sign + "₹" + (a / 1e5).toFixed(a >= 1e6 ? 0 : 1) + " lakh";
    return sign + inr(a);
  };

  // Accepts "12,34,567", "45 lakh", "1.2cr" — people type money the way they say
  // it, and rejecting that is how a calculator loses someone on the first row.
  const parse = (raw) => {
    const t = String(raw || "").toLowerCase().replace(/[, ]/g, "").trim();
    if (!t) return 0;
    // Check the sign *before* stripping non-numerics, or "-5" becomes "5" and an
    // overdraft typed into the bank row would add to assets instead of being
    // ignored. Each box asks how much of one thing you have; debts have a side.
    if (/^[-−]/.test(t)) return 0;
    const n = parseFloat(t.replace(/[^0-9.]/g, ""));
    if (!isFinite(n) || n < 0) return 0;
    if (/(cr|crore)$/.test(t)) return n * 1e7;
    if (/(l|lac|lakh|lakhs)$/.test(t)) return n * 1e5;
    if (/k$/.test(t)) return n * 1e3;
    return n;
  };

  const sum = (side) =>
    inputs.filter((el) => el.dataset.side === side)
          .reduce((t, el) => t + parse(el.value), 0);

  function recalc() {
    const assets = sum("asset");
    const liabilities = sum("liability");
    const net = assets - liabilities;

    assetEl.textContent = inr(assets);
    liabEl.textContent = inr(liabilities);
    netEl.textContent = (net < 0 ? "−₹" : "₹") + Math.abs(Math.round(net)).toLocaleString("en-IN");
    netEl.classList.toggle("neg", net < 0);

    if (!assets && !liabilities) {
      noteEl.textContent = "Assets minus liabilities. Fill in anything above to start.";
      nextEl.hidden = true;
      return;
    }
    noteEl.textContent =
      compact(assets) + " of assets − " + compact(liabilities) + " of liabilities" +
      (net < 0 ? " — you owe more than you own right now." : " = " + compact(net) + ".");

    nextEl.hidden = net <= 0;
    lastNet = net;
  }

  // Carry the figure to the ranking page in sessionStorage, not in the link.
  // A ?nw= query string would send the visitor's net worth to the server in the
  // request line and on to Google Analytics, which runs on public pages — which
  // is precisely what the sentence at the top of this page promises doesn't
  // happen. The href stays plain, so the URL is safe to copy or share.
  if (rankEl) {
    rankEl.addEventListener("click", () => {
      try {
        if (lastNet > 0) sessionStorage.setItem("nw-handoff", String(Math.round(lastNet)));
      } catch (e) {
        /* private mode — the ranking page just opens at its default */
      }
    });
  }

  inputs.forEach((el) => el.addEventListener("input", recalc));
  recalc();
}
