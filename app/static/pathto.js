// "What return would it take?" — runs entirely in the browser.
//
// Mirrors app/pathto.py. Keep the two in step: the Python owns the
// server-rendered reference tables, this owns the live calculator, and if they
// disagree the page contradicts itself in front of the reader.

function initPathTo(cfg) {
  const nowEl = document.getElementById("p-now");
  const targetEl = document.getElementById("p-target");
  const yearsEl = document.getElementById("p-years");
  const saveEl = document.getElementById("p-save");
  const inflEl = document.getElementById("p-inflation");
  const outEl = document.getElementById("p-out");
  if (!nowEl || !targetEl || !outEl) return;

  const num = (el) => {
    const v = parseFloat(String(el.value).replace(/[^0-9.]/g, ""));
    return isFinite(v) ? v : 0;
  };
  const inr = (v) => "₹" + Math.round(v).toLocaleString("en-IN");
  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e7) return "₹" + (v / 1e7).toFixed(a >= 1e8 ? 0 : 2) + " cr";
    if (a >= 1e5) return "₹" + (v / 1e5).toFixed(a >= 1e6 ? 0 : 1) + " lakh";
    return inr(v);
  };

  const futureValue = (current, save, years, pct) => {
    const r = pct / 100;
    let b = current;
    for (let i = 0; i < years; i++) b = b * (1 + r) + save;
    return b;
  };

  // Bisected, not solved: with savings in the mix the rate has no closed form,
  // but the balance is monotonic in it, which is all bisection needs.
  function requiredReturn(current, target, years, save) {
    if (years <= 0 || target <= 0) return null;
    if (futureValue(current, save, years, 0) >= target) return 0;
    if (futureValue(current, save, years, 100) < target) return null;
    let lo = 0, hi = 100;
    for (let i = 0; i < 200 && hi - lo > 1e-6; i++) {
      const mid = (lo + hi) / 2;
      if (futureValue(current, save, years, mid) < target) lo = mid; else hi = mid;
    }
    return hi;
  }

  function yearsToTarget(current, target, save, pct, cap) {
    if (target <= current) return 0;
    const r = pct / 100;
    let b = current;
    for (let y = 1; y <= cap; y++) {
      b = b * (1 + r) + save;
      if (b >= target) return y;
    }
    return null;
  }

  function savingsNeeded(current, target, years, pct) {
    const r = pct / 100;
    const grown = current * Math.pow(1 + r, years);
    if (grown >= target) return 0;
    const factor = r === 0 ? years : (Math.pow(1 + r, years) - 1) / r;
    return (target - grown) / factor;
  }

  function band(pct) {
    for (const b of cfg.bands) if (pct < b[0]) return { label: b[1], note: b[2] };
    const last = cfg.bands[cfg.bands.length - 1];
    return { label: last[1], note: last[2] };
  }

  function render() {
    const current = num(nowEl);
    const target = num(targetEl) * 1e7;      // entered in crore
    const years = Math.max(1, Math.round(num(yearsEl) || 20));
    const save = num(saveEl) * 12;           // entered per month
    const infl = num(inflEl) || 0;
    const deflate = (v, years) => v / Math.pow(1 + infl / 100, years);

    if (target <= 0) {
      outEl.innerHTML = '<p class="muted">Set a target to see what it would take.</p>';
      return;
    }
    if (target <= current) {
      outEl.innerHTML = `
        <div class="p-head p-head--yes">
          <span class="p-answer">You're already there</span>
          <span class="p-sub">${compact(current)} today is past your ${compact(target)} target.</span>
        </div>`;
      return;
    }

    const rate = requiredReturn(current, target, years, save);

    if (rate === null) {
      // Deliberately blunt. A calculator that quotes 340% a year without saying
      // it's impossible is worse than one that refuses.
      const atTwelve = yearsToTarget(current, target, save, 12, 100);
      outEl.innerHTML = `
        <div class="p-head p-head--no">
          <span class="p-answer">Not on this timeline</span>
          <span class="p-sub">
            No return gets ${compact(current)} to ${compact(target)} in ${years} years while
            you add ${compact(save)} a year. The arithmetic has run out, not your ambition.
          </span>
        </div>
        <p class="p-why">
          At a realistic <b>12%</b> a year it would take
          <b>${atTwelve === null ? "more than 100 years" : atTwelve + " years"}</b>.
          Move the date, the target, or what you put away.
        </p>`;
      return;
    }

    const v = band(rate);
    const cls = rate < 9 ? "yes" : rate < 12 ? "ok" : rate < 15 ? "warn" : "no";
    let out = `
      <div class="p-head p-head--${cls}">
        <span class="p-eyebrow">Your portfolio would have to earn</span>
        <span class="p-answer">${rate.toFixed(1)}<em>% a year</em></span>
        <span class="p-sub">
          to turn ${compact(current)} into ${compact(target)} in ${years} years${
            save > 0 ? `, adding ${inr(Math.round(save / 12))} a month` : ""}.
          That's <b>${v.label}</b>.
        </span>
      </div>
      <p class="p-why">${v.note}</p>`;

    // What the target is actually worth. A big rupee figure decades out is mostly
    // inflation, and "₹170 crore" reads as life-changing when the honest version
    // is "₹53 crore of today's money" — a far more modest claim.
    if (infl > 0) {
      const realTarget = deflate(target, years);
      const realGrowth = current > 0
        ? (Math.pow(realTarget / current, 1 / years) - 1) * 100 : null;
      out += `
        <p class="p-real">
          <b>${compact(target)} in ${years} years is worth about ${compact(realTarget)}
          in today's money</b> at ${infl}% inflation.${
            realGrowth !== null
              ? ` So the real claim is ${compact(current)} → ${compact(realTarget)} of
                  today's buying power — <b>${realGrowth.toFixed(1)}% a year after
                  inflation</b>, which is the number worth judging.`
              : ""}
        </p>`;
    }

    // The half only a tracker can answer: is that rate plausible for the
    // portfolio this person actually holds?
    if (cfg.blended !== null && cfg.blended !== undefined) {
      const gap = rate - cfg.blended;
      const verdict =
        gap <= -1
          ? `Your current mix would plausibly earn about <b>${cfg.blended.toFixed(1)}%</b>,
             which is <b>more than you need</b>. You have room to take less risk, not more.`
          : gap <= 1
          ? `Your current mix would plausibly earn about <b>${cfg.blended.toFixed(1)}%</b> —
             roughly what this needs. It's a plan, but with no margin for a bad decade.`
          : `Your current mix would plausibly earn about <b>${cfg.blended.toFixed(1)}%</b>,
             <b>${gap.toFixed(1)} points short</b>. This isn't a shortage of ambition —
             it's an allocation that can't get there. Changing the mix, the target or the
             timeline are the only real levers.`;
      out += `<p class="p-mix">${verdict}</p>`;
    }

    // The levers, priced. Everyone reaches for "earn more"; these are the two
    // that don't depend on markets cooperating.
    const realistic = 12;
    const needSave = savingsNeeded(current, target, years, realistic);
    const yearsAt = yearsToTarget(current, target, save, realistic, 100);
    out += `
      <div class="p-levers">
        <span class="p-lev-lab">If you assume a realistic ${realistic}% instead</span>
        <div class="p-lev-grid">
          <div class="p-lev">
            <span class="p-lev-k">Save this much a month</span>
            <span class="p-lev-v">${inr(Math.ceil(needSave / 12))}</span>
            <span class="p-lev-n">to still hit ${compact(target)} in ${years} years</span>
          </div>
          <div class="p-lev">
            <span class="p-lev-k">Or keep saving ${inr(Math.round(save / 12))} and take</span>
            <span class="p-lev-v">${yearsAt === null ? "100+" : yearsAt}<em> yrs</em></span>
            <span class="p-lev-n">instead of ${years}</span>
          </div>
        </div>
      </div>`;

    // One number reads as a prediction. Small differences in return compound into
    // enormous ones over these horizons — showing the spread is the difference
    // between a projection and a forecast.
    if (current > 0) {
      const around = [rate - 2, rate - 1, rate, rate + 1, rate + 2].filter((r) => r > 0);
      out += `
        <div class="p-band-card">
          <span class="p-lev-lab">What you'd actually end with</span>
          <p class="p-band-note">
            The rate above is an assumption, not a measurement — and over ${years} years a
            point either way changes the answer enormously.
          </p>
          <div class="stand-table-wrap">
            <table class="stand-table">
              <thead><tr>
                <th>If returns are</th><th class="num">You end with</th>
                ${infl > 0 ? `<th class="num">In today's money</th>` : ""}
              </tr></thead>
              <tbody>
                ${around.map((r) => {
                  const end = futureValue(current, save, years, r);
                  const hit = Math.abs(r - rate) < 0.05;
                  return `<tr${hit ? ' class="p-band-hit"' : ""}>
                    <th scope="row">${r.toFixed(1)}%${hit ? " <em>— the rate you need</em>" : ""}</th>
                    <td class="num">${compact(end)}</td>
                    ${infl > 0 ? `<td class="num">${compact(deflate(end, years))}</td>` : ""}
                  </tr>`;
                }).join("")}
              </tbody>
            </table>
          </div>
        </div>`;
    }

    outEl.innerHTML = out;
  }

  [nowEl, targetEl, yearsEl, saveEl, inflEl].forEach((el) => {
    if (!el) return;
    el.addEventListener("input", render);
    el.addEventListener("change", render);
  });
  render();
}
