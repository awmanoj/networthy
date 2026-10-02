// "How much house can I afford?" — runs entirely in the browser.
//
// A property price, a salary and a loan balance are about as personal as this
// app gets, so as with standing.js and retire.js there is no fetch in this file
// and there must never be one.
//
// The arithmetic mirrors app/homeloan.py: the same RBI slabs, the same monthly
// amortisation loop, and the same bisection for "what must I pay each year to
// finish in N". Keep the two in step if either changes.

function initHomeLoan(cfg) {
  const $ = (id) => document.getElementById(id);
  const priceEl = $("hl-price"), stateEl = $("hl-state"), rateEl = $("hl-rate"),
        yearsEl = $("hl-years"), incomeEl = $("hl-income");
  if (!priceEl) return;

  const inr = (v) => "₹" + Math.round(v).toLocaleString("en-IN");
  const compact = (v) => {
    const a = Math.abs(v);
    if (a >= 1e7) return "₹" + (a / 1e7).toFixed(2) + " cr";
    if (a >= 1e5) return "₹" + (a / 1e5).toFixed(1) + " lakh";
    return inr(a);
  };
  const num = (el, fallback) => {
    if (!el) return fallback;
    const t = String(el.value || "").toLowerCase().replace(/[, ]/g, "");
    const n = parseFloat(t.replace(/[^0-9.]/g, ""));
    if (!isFinite(n) || n < 0) return fallback;
    if (/(cr|crore)$/.test(t)) return n * 1e7;
    if (/(l|lac|lakh|lakhs)$/.test(t)) return n * 1e5;
    return n;
  };

  // --- the same maths as app/homeloan.py ------------------------------------
  const LTV = cfg.ltv;                 // [[ceiling, ratio, label], …]

  function maxLoan(price) {
    for (const [ceiling, ratio, label] of LTV) {
      const loan = price * ratio;
      if (loan <= ceiling) return { loan, ratio, label };
    }
    const [, ratio, label] = LTV[LTV.length - 1];
    return { loan: price * ratio, ratio, label };
  }

  function emi(principal, ratePct, years) {
    const n = Math.round(years * 12), r = ratePct / 1200;
    if (principal <= 0 || n <= 0) return 0;
    if (r === 0) return principal / n;
    const f = Math.pow(1 + r, n);
    return (principal * r * f) / (f - 1);
  }

  function run(principal, ratePct, years, annualPrepay) {
    const r = ratePct / 1200, instalment = emi(principal, ratePct, years);
    const cap = Math.round(years * 12) + 1200;
    let balance = principal, interest = 0, months = 0;
    while (balance > 0.005 && months < cap) {
      const due = balance * r;
      const pay = Math.min(instalment, balance + due);
      balance = balance + due - pay;
      interest += due;
      months++;
      if (months % 12 === 0 && annualPrepay > 0 && balance > 0) {
        balance -= Math.min(annualPrepay, balance);
      }
    }
    return { months, interest, emi: instalment };
  }

  // Bisected, not solved: the loop rounds to whole months and applies the lump
  // sum in discrete jumps, so it doesn't invert cleanly. It is monotonic in the
  // prepayment, which is all bisection needs.
  function prepayFor(principal, ratePct, years, targetYears) {
    const target = Math.round(targetYears * 12);
    if (target >= Math.round(years * 12)) return 0;
    if (run(principal, ratePct, years, principal).months > target) return null;
    let lo = 0, hi = principal;
    for (let i = 0; i < 60; i++) {
      const mid = (lo + hi) / 2;
      if (run(principal, ratePct, years, mid).months <= target) hi = mid; else lo = mid;
    }
    return hi;
  }

  // --- render ----------------------------------------------------------------
  function recalc() {
    const price = num(priceEl, 1e7);
    const ratePct = num(rateEl, cfg.rate);
    const years = Math.max(1, num(yearsEl, cfg.years));
    // Effective rate comes from the server, already including any cess and
    // surcharge — those apply to the stamp duty, not the property value, and
    // recomputing that here is how the two would drift apart.
    const duty = cfg.duty[stateEl.value] || cfg.duty[cfg.defaultState];
    const dutyPct = duty.effective;

    const { loan, ratio, label } = maxLoan(price);
    const down = price - loan;
    const dutyAmt = (price * dutyPct) / 100;
    const cash = down + dutyAmt;

    $("hl-loan").textContent = inr(loan);
    $("hl-loan-note").textContent = `${Math.round(ratio * 100)}% of the price — RBI's cap for a loan ${label}`;
    $("hl-cash").textContent = inr(cash);
    $("hl-cash-note").textContent =
      `${compact(down)} down payment + ${compact(dutyAmt)} stamp duty & registration ` +
      `(${dutyPct.toFixed(2)}%)` + (duty.verified ? "" : " — rate not recently verified");

    const base = run(loan, ratePct, years, 0);
    $("hl-emi").textContent = inr(base.emi);
    $("hl-emi-note").textContent =
      `${inr(loan + base.interest)} repaid in total — ${compact(base.interest)} of it interest`;

    // Income check: whichever ceiling binds first is the real one.
    const income = num(incomeEl, 0);
    const verdict = $("hl-verdict");
    if (income > 0) {
      const allowed = (income * cfg.foir) / 100;
      const share = (base.emi / income) * 100;
      const ok = base.emi <= allowed;
      verdict.hidden = false;
      verdict.className = "hl-verdict " + (ok ? "ok" : "no");
      verdict.textContent = ok
        ? `That EMI is ${share.toFixed(0)}% of your monthly income — inside the ~${cfg.foir}% most lenders allow.`
        : `That EMI is ${share.toFixed(0)}% of your monthly income. Lenders usually stop near ${cfg.foir}%, so expect to be offered less — around ${compact(allowed)} a month.`;
    } else {
      verdict.hidden = true;
    }

    // The ladder.
    const rows = [`<tr><td>Nothing</td><td class="num">${years.toFixed(0)} yrs</td>` +
                  `<td class="num">${compact(base.interest)}</td><td class="num">—</td></tr>`];
    for (const target of cfg.targets) {
      if (target * 12 >= base.months) continue;
      const annual = prepayFor(loan, ratePct, years, target);
      if (annual === null) continue;
      const r2 = run(loan, ratePct, years, annual);
      rows.push(
        `<tr><td>${inr(annual)}<span class="hl-pm">${inr(annual / 12)}/month</span></td>` +
        `<td class="num">${(r2.months / 12).toFixed(0)} yrs</td>` +
        `<td class="num">${compact(r2.interest)}</td>` +
        `<td class="num gain">${compact(base.interest - r2.interest)}</td></tr>`);
    }
    $("hl-ladder").innerHTML = rows.join("");

    // The classic rule of thumb, priced.
    const extra = run(loan, ratePct, years, base.emi);
    $("hl-thumb").textContent =
      `One extra EMI a year (${inr(base.emi)}) ends it in ${(extra.months / 12).toFixed(1)} years ` +
      `and saves ${compact(base.interest - extra.interest)}.`;
  }

  [priceEl, stateEl, rateEl, yearsEl, incomeEl].forEach(
    (el) => el && el.addEventListener("input", recalc));
  stateEl.addEventListener("change", recalc);
  recalc();
}
