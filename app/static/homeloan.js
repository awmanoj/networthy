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
        yearsEl = $("hl-years"), incomeEl = $("hl-income"), sanctionEl = $("hl-sanction");
  const poBalance = $("po-balance"), poRate = $("po-rate"), poYears = $("po-years"),
        poEmi = $("po-emi"), poPrepay = $("po-prepay"), poParked = $("po-parked");
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

  // RBI's slab caps the bank; it does not oblige it. A sanction letter wins when
  // there is one, clamped to the price; the cap is reported alongside.
  function loanAmount(price, sanctioned) {
    const { loan: cap, ratio, label } = maxLoan(price);
    if (!sanctioned || sanctioned <= 0) {
      return { loan: cap, ratio, label, cap, sanctioned: false, overCap: false };
    }
    const loan = Math.min(sanctioned, price);
    return { loan, ratio: price > 0 ? loan / price : 0, label, cap,
             sanctioned: true, overCap: loan > cap + 0.5 };
  }

  // Closing a loan you already have. Interest accrues on balance − parked each
  // month; the payment (EMI + monthly prepay) is applied to it. Closes when the
  // net outstanding is nil. Never closes if the payment doesn't cover interest.
  function payoff(balance, ratePct, emiPaid, monthlyPrepay = 0, parked = 0) {
    const r = ratePct / 1200;
    const netStart = Math.max(balance - Math.max(parked, 0), 0);
    let net = netStart, interest = 0, months = 0;
    const payEach = Math.max(emiPaid, 0) + Math.max(monthlyPrepay, 0);
    while (net > 0.005 && months < cfg.payoffMaxMonths) {
      const due = net * r;
      if (payEach <= due) return { months: null, interest: null, closes: false, netStart };
      const pay = Math.min(payEach, net + due);
      net = net + due - pay;
      interest += due;
      months++;
    }
    const closes = net <= 0.005;
    return { months: closes ? months : null, interest: closes ? interest : null, closes, netStart };
  }

  function payoffScenarios(balance, ratePct, yearsLeft, emiPaid, monthlyPrepay, parked) {
    const sched = emi(balance, ratePct, yearsLeft);
    const paid = (!emiPaid || emiPaid <= 0) ? sched : emiPaid;
    const base = payoff(balance, ratePct, sched);
    const row = (label, run) => ({
      label, months: run.months, interest: run.interest, closes: run.closes,
      saved: (run.closes && base.closes) ? base.interest - run.interest : null,
    });
    const rows = [row("Scheduled EMI, nothing extra", base)];
    const emiChanged = Math.abs(paid - sched) > 0.5;
    if (emiChanged) rows.push(row("EMI of this size alone", payoff(balance, ratePct, paid)));
    if (monthlyPrepay > 0) rows.push(row("Monthly prepayment alone", payoff(balance, ratePct, sched, monthlyPrepay)));
    if (parked > 0) rows.push(row("Parked in the OD account alone", payoff(balance, ratePct, sched, 0, parked)));
    const combined = payoff(balance, ratePct, paid, monthlyPrepay, parked);
    const levers = (emiChanged ? 1 : 0) + (monthlyPrepay > 0 ? 1 : 0) + (parked > 0 ? 1 : 0);
    if (levers > 1) rows.push(row("All of the above together", combined));
    return { scheduledEmi: sched, emiPaid: paid, base, combined, rows, levers };
  }

  // --- render ----------------------------------------------------------------
  const yrsMo = (m) => {
    const y = Math.floor(m / 12), mo = m % 12;
    return `${y} yrs` + (mo ? ` ${mo} mo` : "");
  };

  function recalc() {
    const price = num(priceEl, 1e7);
    const ratePct = num(rateEl, cfg.rate);
    const years = Math.max(1, num(yearsEl, cfg.years));
    // Effective rate comes from the server, already including any cess and
    // surcharge — those apply to the stamp duty, not the property value, and
    // recomputing that here is how the two would drift apart.
    const duty = cfg.duty[stateEl.value] || cfg.duty[cfg.defaultState];
    const dutyPct = duty.effective;

    const la = loanAmount(price, num(sanctionEl, 0));
    const { loan, ratio, label } = la;
    const down = price - loan;
    const dutyAmt = (price * dutyPct) / 100;
    const cash = down + dutyAmt;

    $("hl-loan").textContent = inr(loan);
    $("hl-loan-eyebrow").textContent = la.sanctioned ? "Your sanctioned loan" : "Bank can lend";
    $("hl-loan-note").textContent = la.sanctioned
      ? (la.overCap
          ? `${Math.round(ratio * 100)}% of the price — above RBI's ${Math.round((la.cap / price) * 100)}% cap for a loan ${label}; check the letter`
          : `${Math.round(ratio * 100)}% of the price — RBI's cap would have allowed ${compact(la.cap)}`)
      : `${Math.round(ratio * 100)}% of the price — RBI's cap for a loan ${label}`;
    $("hl-cash").textContent = inr(cash);
    $("hl-cash-note").textContent =
      `${compact(down)} down payment` + (la.sanctioned ? " (price − your loan)" : "") +
      ` + ${compact(dutyAmt)} stamp duty & registration ` +
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

  function recalcPayoff() {
    if (!poBalance) return;
    const balance = num(poBalance, 0);
    const ratePct = num(poRate, cfg.rate);
    const years = Math.max(1, num(poYears, cfg.years));
    const s = payoffScenarios(balance, ratePct, years, num(poEmi, 0), num(poPrepay, 0), num(poParked, 0));
    poEmi.placeholder = inr(s.scheduledEmi).replace("₹", "");
    $("po-sched").textContent = inr(s.scheduledEmi);
    $("po-sched-note").textContent = `what the bank asks for over ${years.toFixed(0)} years`;

    const { base, combined } = s;
    const savedEl = $("po-saved");
    savedEl.classList.remove("loss");
    savedEl.classList.add("gain");
    if (balance <= 0 || !base.closes) {
      $("po-ends").textContent = "—";
      $("po-ends-note").textContent = "enter what you still owe, the rate and the years left";
      savedEl.textContent = "—";
      $("po-saved-note").textContent = "";
    } else if (!combined.closes) {
      $("po-ends").textContent = "Never";
      $("po-ends-note").textContent = "that EMI doesn't even cover the interest — the loan grows";
      savedEl.textContent = "—";
      $("po-saved-note").textContent = `the scheduled EMI is ${inr(s.scheduledEmi)}`;
    } else {
      const saved = base.interest - combined.interest;
      $("po-ends").textContent = yrsMo(combined.months);
      $("po-ends-note").textContent = s.levers
        ? `instead of ${yrsMo(base.months)} on the bank's schedule`
        : "on the bank's schedule — set a lever above to see what changes";
      // An EMI below the scheduled one can still close (the other levers carry
      // it) but costs more; say so rather than paint a negative saving green.
      if (saved < -0.5) {
        savedEl.classList.remove("gain");
        savedEl.classList.add("loss");
        savedEl.textContent = `−${inr(-saved)}`;
        $("po-saved-note").textContent =
          `you'd pay ${compact(-saved)} MORE interest — that EMI is below the ${inr(s.scheduledEmi)} scheduled`;
      } else {
        savedEl.textContent = inr(saved);
        $("po-saved-note").textContent =
          `${compact(combined.interest)} interest instead of ${compact(base.interest)}`;
      }
    }

    const rows = s.rows.map((r, i) => {
      const cls = (i === s.rows.length - 1 && s.levers > 1) ? ' class="hl-total"' : "";
      if (!r.closes) {
        return `<tr${cls}><td>${r.label}</td><td class="num loss" colspan="3">Never — the payment doesn't cover the interest</td></tr>`;
      }
      const savedCell = i === 0 ? '<td class="num gain">—</td>'
        : r.saved < -0.5 ? `<td class="num loss">−${inr(-r.saved)}</td>`
        : `<td class="num gain">${inr(r.saved)}</td>`;
      return `<tr${cls}><td>${r.label}</td><td class="num">${yrsMo(r.months)}</td>` +
             `<td class="num">${inr(r.interest)}</td>${savedCell}</tr>`;
    });
    $("po-rows").innerHTML = rows.join("");
  }

  [priceEl, stateEl, rateEl, yearsEl, incomeEl, sanctionEl].forEach(
    (el) => el && el.addEventListener("input", recalc));
  stateEl.addEventListener("change", recalc);
  [poBalance, poRate, poYears, poEmi, poPrepay, poParked].forEach(
    (el) => el && el.addEventListener("input", recalcPayoff));
  recalc();
  recalcPayoff();
}
