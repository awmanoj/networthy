// "How much do I need to retire?" — runs entirely in the browser.
//
// Nothing here talks to the server: the visitor's net worth and spending are
// typed into a public page, so the only defensible design is that they never
// leave the tab. The arithmetic mirrors app/expenses.py (corpus = annual spend
// / withdrawal rate) and app/projection.py's year loop (how long a corpus
// survives an inflating draw) — keep the three in step if any of them changes.

function initRetire(cfg) {
  const nwEl = document.getElementById("r-networth");
  const spendEl = document.getElementById("r-spend");
  const unitEl = document.getElementById("r-unit");
  const retEl = document.getElementById("r-return");
  const inflEl = document.getElementById("r-inflation");
  const rowsEl = document.getElementById("r-rows");
  const verdictEl = document.getElementById("r-verdict");
  if (!nwEl || !spendEl || !rowsEl) return;

  const RATES = cfg.rates;
  const inr = (v) => "₹" + Math.round(v).toLocaleString("en-IN");
  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e7) return "₹" + (v / 1e7).toFixed(a >= 1e8 ? 0 : 2) + " cr";
    if (a >= 1e5) return "₹" + (v / 1e5).toFixed(a >= 1e6 ? 0 : 1) + " lakh";
    return inr(v);
  };
  const num = (el) => {
    const v = parseFloat(String(el.value).replace(/[^0-9.]/g, ""));
    return isFinite(v) ? v : 0;
  };

  // Same loop as projection.project(): the balance earns the return, then the
  // (inflating) draw is taken at year end. Returns null if it outlives the cap.
  function yearsLasting(corpus, annualDraw, returnPct, inflPct, cap) {
    const r = returnPct / 100, f = inflPct / 100;
    let balance = corpus;
    for (let i = 0; i < cap; i++) {
      balance = balance + balance * r - annualDraw * Math.pow(1 + f, i);
      if (balance <= 0) return i;
    }
    return null;
  }

  function render() {
    const netWorth = num(nwEl);
    const perMonth = unitEl.value === "year" ? num(spendEl) / 12 : num(spendEl);
    const annual = perMonth * 12;
    const returnPct = num(retEl) || cfg.defaultReturn;
    const inflPct = num(inflEl) || cfg.defaultInflation;

    if (annual <= 0) {
      rowsEl.innerHTML = "";
      verdictEl.innerHTML =
        '<p class="muted">Enter what you spend in a month to see the corpus it takes.</p>';
      return;
    }

    rowsEl.innerHTML = RATES.map((rate) => {
      const needed = annual / (rate.pct / 100);
      const gap = needed - netWorth;
      const pctThere = needed ? Math.min(100, (netWorth / needed) * 100) : 0;
      const state = gap <= 0 ? "ok" : "short";
      return `
        <div class="r-row r-row--${state}">
          <div class="r-rate">
            <b class="num">${rate.pct}%</b>
            <span class="r-mult">${Math.round(100 / rate.pct)}×</span>
          </div>
          <div class="r-meat">
            <div class="r-head">
              <span class="r-label">${rate.label}</span>
              <span class="r-corpus num">${compact(needed)}</span>
            </div>
            <div class="fire-bar"><span style="width:${pctThere.toFixed(1)}%"></span></div>
            <div class="r-foot">
              <span class="muted">${rate.note}</span>
              <span class="r-gap">${
                gap <= 0
                  ? "covered · " + compact(-gap) + " spare"
                  : compact(gap) + " short"
              }</span>
            </div>
          </div>
        </div>`;
    }).join("");

    // The reverse read: what rate is this person actually running today? It's
    // the number they didn't ask for and usually the one that matters.
    if (netWorth > 0) {
      const impliedRate = (annual / netWorth) * 100;
      const lasts = yearsLasting(netWorth, annual, returnPct, inflPct, 60);
      const verdict =
        lasts === null
          ? `at ${returnPct}% returns and ${inflPct}% inflation it outlasts 60 years`
          : `at ${returnPct}% returns and ${inflPct}% inflation it runs dry in about <strong>${lasts} years</strong>`;
      verdictEl.innerHTML = `
        <p class="r-implied">
          Retiring today on ${compact(netWorth)} while spending ${inr(perMonth)} a month
          means withdrawing <strong>${impliedRate.toFixed(1)}%</strong> a year —
          ${verdict}.
        </p>
        <p class="muted r-implied-note">
          Anything at or under 3% is the defensible range for India. Above 4% you are
          relying on returns showing up in the right order, which is not something you
          control.
        </p>`;
    } else {
      verdictEl.innerHTML =
        '<p class="muted">Add what you have saved to see where you stand against each line.</p>';
    }
  }

  [nwEl, spendEl, unitEl, retEl, inflEl].forEach((el) => {
    el.addEventListener("input", render);
    el.addEventListener("change", render);
  });
  render();
}
