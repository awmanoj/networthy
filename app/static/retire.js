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
  const yearsEl = document.getElementById("r-years");
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

  // The corpus that survives exactly `years` at these assumptions — the mirror
  // of projection.corpus_requirement(), bisected for the same reason: the loop
  // is monotonic in the starting corpus but doesn't invert cleanly.
  function corpusFor(annualDraw, returnPct, inflPct, years) {
    const survives = (c) => {
      const n = yearsLasting(c, annualDraw, returnPct, inflPct, years);
      return n === null;
    };
    let hi = Math.max(annualDraw, 1);
    for (let i = 0; i < 200 && !survives(hi); i++) hi *= 2;
    let lo = 0;
    while (hi - lo > Math.max(1000, hi * 1e-6)) {
      const mid = (lo + hi) / 2;
      if (survives(mid)) hi = mid; else lo = mid;
    }
    return hi;
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

    const horizon = Math.max(1, Math.round(num(yearsEl) || cfg.defaultYears));

    rowsEl.innerHTML = RATES.map((rate) => {
      const needed = annual / (rate.pct / 100);
      const gap = needed - netWorth;
      const pctThere = needed ? Math.min(100, (netWorth / needed) * 100) : 0;
      const state = gap <= 0 ? "ok" : "short";
      // How that rule-of-thumb corpus actually behaves under the assumptions the
      // visitor entered. This is what makes the return/inflation inputs bite on
      // every row — the multiple itself never moves, but its consequence does.
      const lasts = yearsLasting(needed, annual, returnPct, inflPct, cfg.cap);
      const lastsLabel =
        lasts === null ? `lasts ${cfg.cap}+ yrs` : `lasts ~${lasts} yrs`;
      const lastsClass = lasts === null || lasts >= horizon ? "ok" : "short";

      // What the surplus buys, in years. Only stated when both durations are real
      // measurements: if your own corpus outlasts the cap, the "extra" would be
      // (cap − lasts), which is arithmetic on a display limit rather than on
      // anything the model computed. Quoting it would invent precision — the true
      // answer there is "indefinitely", handled in the verdict below.
      const mineLasts = yearsLasting(netWorth, annual, returnPct, inflPct, cfg.cap);
      let bonus = "";
      if (gap <= 0 && lasts !== null) {
        // "doesn't run dry" would be an overclaim: outlasting the cap is not the
        // same as lasting forever, and whether it truly never depletes depends on
        // the draw versus the *real* return — which the verdict below works out.
        bonus = mineLasts === null
          ? ` · yours lasts past ${cfg.cap} yrs`
          : ` · buys ${mineLasts - lasts} more yrs`;
      }
      return `
        <div class="r-row r-row--${state}">
          <div class="r-rate">
            <b class="num">${rate.pct}%</b>
            <span class="r-mult">${Math.round(100 / rate.pct)}×</span>
          </div>
          <div class="r-meat">
            <div class="r-head">
              <span class="r-label">${rate.label}</span>
              <span class="r-corpus num">${compact(needed)}
                <em class="r-lasts r-lasts--${lastsClass}">${lastsLabel}</em>
              </span>
            </div>
            <div class="fire-bar"><span style="width:${pctThere.toFixed(1)}%"></span></div>
            <div class="r-foot">
              <span class="muted">${rate.note}</span>
              <span class="r-gap">${
                gap <= 0
                  ? "covered · " + compact(-gap) + " spare" + bonus
                  : compact(gap) + " short"
              }</span>
            </div>
          </div>
        </div>`;
    }).join("");

    // The modelled answer, as opposed to the rule of thumb above: the corpus
    // that actually funds this spending for `horizon` years at the entered
    // return and inflation. Unlike the multiples, this moves with every input —
    // which is the point of having the inputs on the page.
    const modelled = corpusFor(annual, returnPct, inflPct, horizon);
    const modelledRate = (annual / modelled) * 100;
    let out = `
      <p class="r-implied">
        At <strong>${returnPct}%</strong> returns and <strong>${inflPct}%</strong>
        inflation, funding ${inr(perMonth)} a month for <strong>${horizon} years</strong>
        takes about <strong>${compact(modelled)}</strong> — a
        ${modelledRate.toFixed(1)}% withdrawal rate.
      </p>`;

    // The reverse read: what rate is this person actually running today? It's
    // the number they didn't ask for and usually the one that matters.
    if (netWorth > 0) {
      const impliedRate = (annual / netWorth) * 100;
      const lasts = yearsLasting(netWorth, annual, returnPct, inflPct, cfg.cap);
      // Real return, not nominal minus inflation — the difference matters at
      // these rates. Withdraw less than this and the corpus grows instead of
      // depleting, which is why "how many years" has no answer: it's every year.
      const realReturn = ((1 + returnPct / 100) / (1 + inflPct / 100) - 1) * 100;
      const verdict =
        lasts === null
          ? (impliedRate < realReturn
              ? `it never runs dry — you'd be drawing ${impliedRate.toFixed(1)}% while the
                 portfolio earns about <strong>${realReturn.toFixed(1)}% after inflation</strong>,
                 so the corpus grows rather than shrinks`
              : `it lasts beyond ${cfg.cap} years`)
          : `it runs dry in about <strong>${lasts} years</strong>`;
      out += `
        <p class="r-implied r-implied--second">
          Retiring today on ${compact(netWorth)} while spending ${inr(perMonth)} a month
          means withdrawing <strong>${impliedRate.toFixed(1)}%</strong> a year — at these
          assumptions ${verdict}.
        </p>`;
    }

    out += `
      <p class="muted r-implied-note">
        Why this figure is usually smaller than the multiples above: it spends the corpus
        down to nothing at the end of ${horizon} years, while 25×/33×/40× aim to leave it
        intact indefinitely. And the multiples are fixed rules of thumb, so they don't move
        when you change the boxes — what moves is this figure, and how long each of those
        corpuses actually survives.
      </p>`;
    verdictEl.innerHTML = out;
  }

  [nwEl, spendEl, unitEl, retEl, inflEl, yearsEl].forEach((el) => {
    if (!el) return;
    el.addEventListener("input", render);
    el.addEventListener("change", render);
  });
  render();
}
