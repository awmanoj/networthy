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

  // Balance left at the end of `years`, or 0 if it died on the way. The
  // "leave to your kids" number — and, deflated, the only honest way to show it:
  // a nominal balance 40 years out is mostly inflation.
  function endingBalance(corpus, annualDraw, returnPct, inflPct, years) {
    const r = returnPct / 100, f = inflPct / 100;
    let balance = corpus;
    for (let i = 0; i < years; i++) {
      balance = balance + balance * r - annualDraw * Math.pow(1 + f, i);
      if (balance <= 0) return 0;
    }
    return balance;
  }

  // The largest annual spend this corpus supports for exactly `years`. Bisected
  // for the same reason as corpusFor: spending more shortens the life
  // monotonically, but the loop doesn't invert cleanly.
  function maxSpend(corpus, returnPct, inflPct, years) {
    const survives = (draw) =>
      yearsLasting(corpus, draw, returnPct, inflPct, years) === null;
    let hi = Math.max(corpus, 1);
    for (let i = 0; i < 200 && survives(hi); i++) hi *= 2;
    let lo = 0;
    while (hi - lo > Math.max(100, hi * 1e-7)) {
      const mid = (lo + hi) / 2;
      if (survives(mid)) lo = mid; else hi = mid;
    }
    return lo;
  }

  // What you'd have to put away each year for `years` more years to close a
  // shortfall. The target inflates while you save — retiring later costs more in
  // rupees, because the same lifestyle does — so the goalpost moves with you.
  function contributionPlan(needToday, netWorth, returnPct, inflPct, years) {
    const r = returnPct / 100, f = inflPct / 100;
    const target = needToday * Math.pow(1 + f, years);
    const grown = netWorth * Math.pow(1 + r, years);
    if (grown >= target) return { years, annual: 0, alreadyThere: true };
    // Future value of an ordinary annuity, solved for the payment.
    const factor = r === 0 ? years : (Math.pow(1 + r, years) - 1) / r;
    return { years, annual: (target - grown) / factor, alreadyThere: false };
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

      // No "spare" here any more. A surplus against a bar you've already cleared
      // describes the bar, not your retirement — and it was largest on the row
      // this page argues against, because that row asks least. The duration
      // beside it ranks the standards the right way round.
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
                gap <= 0 ? "✓ you clear this" : compact(gap) + " short"
              }</span>
            </div>
          </div>
        </div>`;
    }).join("");

    // --- The answer -------------------------------------------------------
    //
    // Everything above this is reference material. This is the question people
    // actually arrive with: do I have enough? It's a yes or a no first, and
    // then the thing to do about it — headroom if yes, a route back if no.
    if (netWorth <= 0) {
      verdictEl.innerHTML =
        '<p class="muted">Add what you have saved to see whether it\'s enough.</p>';
      return;
    }

    const needToday = corpusFor(annual, returnPct, inflPct, horizon);
    const enough = netWorth >= needToday;
    const impliedRate = (annual / netWorth) * 100;
    const realReturn = ((1 + returnPct / 100) / (1 + inflPct / 100) - 1) * 100;
    const deflate = (v) => v / Math.pow(1 + inflPct / 100, horizon);

    let out = `
      <div class="v-head v-head--${enough ? "yes" : "no"}">
        <span class="v-answer">${enough ? "Yes — you have enough" : "Not yet"}</span>
        <span class="v-sub">
          ${inr(perMonth)} a month for ${horizon} years needs about
          <b>${compact(needToday)}</b>. You have <b>${compact(netWorth)}</b>.
        </span>
      </div>`;

    if (enough) {
      const surplus = netWorth - needToday;
      const canSpend = maxSpend(netWorth, returnPct, inflPct, horizon);
      const leftOver = endingBalance(netWorth, annual, returnPct, inflPct, horizon);
      out += `
        <div class="v-grid">
          <div class="v-item">
            <span class="v-lab">Ahead by</span>
            <span class="v-val">${compact(surplus)}</span>
            <span class="v-note">more than the ${horizon} years require</span>
          </div>
          <div class="v-item">
            <span class="v-lab">You could spend up to</span>
            <span class="v-val">${inr(Math.floor(canSpend / 12))}<em>/mo</em></span>
            <span class="v-note">
              ${canSpend > annual
                 ? "vs " + inr(perMonth) + " today — " +
                   Math.round((canSpend / annual - 1) * 100) + "% more"
                 : "about what you spend now"}
            </span>
          </div>
          <div class="v-item">
            <span class="v-lab">Left after ${horizon} years</span>
            <span class="v-val">${compact(deflate(leftOver))}</span>
            <span class="v-note">in today's money, still spending ${inr(perMonth)}/mo</span>
          </div>
        </div>
        <p class="v-why">
          You withdraw <b>${impliedRate.toFixed(1)}%</b> a year.
          ${impliedRate < realReturn
            ? `That's below your <b>${realReturn.toFixed(1)}% real return</b> (${returnPct}% growth
               less ${inflPct}% inflation), so the corpus grows rather than shrinks — there's no
               year it runs out.`
            : `Your real return is <b>${realReturn.toFixed(1)}%</b>, so you are drawing down
               capital — it lasts the ${horizon} years, but it does end.`}
        </p>`;
    } else {
      const shortfall = needToday - netWorth;
      const plans = [5, 10, 15]
        .map((y) => contributionPlan(needToday, netWorth, returnPct, inflPct, y))
        .filter((p) => !p.alreadyThere);
      const waitYears = [5, 10, 15, 20, 25, 30].find(
        (y) => contributionPlan(needToday, netWorth, returnPct, inflPct, y).alreadyThere);

      out += `
        <div class="v-grid">
          <div class="v-item v-item--gap">
            <span class="v-lab">Short by</span>
            <span class="v-val">${compact(shortfall)}</span>
            <span class="v-note">as a lump sum today</span>
          </div>
          <div class="v-item">
            <span class="v-lab">Or spend less</span>
            <span class="v-val">${inr(Math.floor(maxSpend(netWorth, returnPct, inflPct, horizon) / 12))}<em>/mo</em></span>
            <span class="v-note">what ${compact(netWorth)} supports for ${horizon} years</span>
          </div>
          ${waitYears ? `
          <div class="v-item">
            <span class="v-lab">Or wait</span>
            <span class="v-val">${waitYears}<em> yrs</em></span>
            <span class="v-note">adding nothing — growth alone gets you there</span>
          </div>` : ""}
        </div>`;

      if (plans.length) {
        out += `
          <div class="v-plans">
            <span class="v-lab">Or invest, and retire later</span>
            <table class="v-plan-table">
              <thead><tr><th>Keep investing for</th><th class="num">Each month</th><th class="num">Each year</th></tr></thead>
              <tbody>
                ${plans.map((p) => `
                  <tr>
                    <td>${p.years} more years</td>
                    <td class="num">${inr(Math.ceil(p.annual / 12))}</td>
                    <td class="num">${compact(p.annual)}</td>
                  </tr>`).join("")}
              </tbody>
            </table>
            <p class="v-note-full">
              The target moves while you save — ${horizon} years of the same lifestyle costs more
              in future rupees — so these already account for the goalpost shifting.
            </p>
          </div>`;
      }
    }
    verdictEl.innerHTML = out;
  }

  [nwEl, spendEl, unitEl, retEl, inflEl, yearsEl].forEach((el) => {
    if (!el) return;
    el.addEventListener("input", render);
    el.addEventListener("change", render);
  });
  render();
}
