// "How much house can I afford?" — runs entirely in the browser.
//
// A property price, a salary and a loan balance are about as personal as this
// app gets, so as with standing.js and retire.js there is no fetch in this file
// and there must never be one.
//
// The arithmetic mirrors app/homeloan.py: the same RBI slabs, the same single
// monthly loop (`payoff`) that the lever table, the ladder and the year-by-year
// schedule are all read off, and the same bisection for "what must I pay a
// month to finish in N". Keep the two in step if either changes.

function initHomeLoan(cfg) {
  const $ = (id) => document.getElementById(id);
  const priceEl = $("hl-price"), stateEl = $("hl-state"), sanctionEl = $("hl-sanction"),
        amountEl = $("hl-amount"), rateEl = $("hl-rate"), yearsEl = $("hl-years"),
        emiEl = $("po-emi"), prepayEl = $("po-prepay"), parkedEl = $("po-parked"),
        incomeEl = $("hl-income"), relinkEl = $("hl-relink");
  const modeBuy = $("hl-mode-buy"), modeHave = $("hl-mode-have");
  if (!priceEl || !amountEl) return;

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
  const yrsMo = (m) => {
    const y = Math.floor(m / 12), mo = m % 12;
    return `${y} yrs` + (mo ? ` ${mo} mo` : "");
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

  function emi(principal, ratePct, years) {
    const n = Math.round(years * 12), r = ratePct / 1200;
    if (principal <= 0 || n <= 0) return 0;
    if (r === 0) return principal / n;
    const f = Math.pow(1 + r, n);
    return (principal * r * f) / (f - 1);
  }

  // The one loop. Interest accrues on balance − parked each month; the payment
  // (EMI + monthly extra) is applied to it. Returns the year-by-year schedule.
  // Never closes if the payment doesn't cover interest.
  function payoff(balance, ratePct, emiPaid, monthlyPrepay = 0, parked = 0) {
    const r = ratePct / 1200;
    parked = Math.max(parked, 0);
    const netStart = Math.max(balance - parked, 0);
    let net = netStart, interest = 0, months = 0;
    const payEach = Math.max(emiPaid, 0) + Math.max(monthlyPrepay, 0);
    const yearly = [];
    let yOpen = net + parked, yPaid = 0, yInt = 0;
    const closeYear = () => yearly.push({ year: yearly.length + 1, opening: yOpen, paid: yPaid,
      interest: yInt, principal: yPaid - yInt, closing: net + parked, net });
    while (net > 0.005 && months < cfg.payoffMaxMonths) {
      const due = net * r;
      if (payEach <= due) return { months: null, interest: null, closes: false, netStart, yearly: [] };
      const pay = Math.min(payEach, net + due);
      net = net + due - pay;
      interest += due;
      yPaid += pay; yInt += due;
      months++;
      if (months % 12 === 0) { closeYear(); yOpen = net + parked; yPaid = 0; yInt = 0; }
    }
    if (months % 12) closeYear();
    const closes = net <= 0.005;
    return { months: closes ? months : null, interest: closes ? interest : null, closes, netStart, yearly };
  }

  // Extra per month, on top of emiPaid, to close within targetYears. Bisected:
  // the loop rounds to whole months so it doesn't invert cleanly; it is
  // monotonic in the prepayment, which is all bisection needs.
  function prepayFor(balance, ratePct, emiPaid, targetYears, parked) {
    const target = Math.round(targetYears * 12);
    const base = payoff(balance, ratePct, emiPaid, 0, parked);
    if (base.closes && base.months <= target) return 0;
    let lo = 0, hi = Math.max(balance, 1);
    const top = payoff(balance, ratePct, emiPaid, hi, parked);
    if (!top.closes || top.months > target) return null;
    for (let i = 0; i < 60; i++) {
      const mid = (lo + hi) / 2;
      const run = payoff(balance, ratePct, emiPaid, mid, parked);
      if (run.closes && run.months <= target) hi = mid; else lo = mid;
    }
    return hi;
  }

  function ladder(balance, ratePct, emiPaid, parked) {
    const base = payoff(balance, ratePct, emiPaid, 0, parked);
    if (!base.closes) return [];
    const rows = [{ years: base.months / 12, monthly: 0, interest: base.interest, saved: 0, months: base.months, base: true }];
    for (const target of cfg.targets) {
      if (target * 12 >= base.months) continue;
      const monthly = prepayFor(balance, ratePct, emiPaid, target, parked);
      if (monthly === null) continue;
      const run = payoff(balance, ratePct, emiPaid, monthly, parked);
      rows.push({ years: run.months / 12, monthly, interest: run.interest,
                  saved: base.interest - run.interest, months: run.months, base: false });
    }
    return rows;
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

  // --- the house → the loan ----------------------------------------------------
  // The amount is linked to the house until the visitor types over it (or
  // switches to "I already have a loan"); a small link restores the link.
  let linked = true;
  const buying = () => !modeHave || !modeHave.checked;

  function recalcHouse() {
    const price = num(priceEl, 1e7);
    const duty = cfg.duty[stateEl.value] || cfg.duty[cfg.defaultState];
    const la = loanAmount(price, num(sanctionEl, 0));
    const down = price - la.loan;
    const dutyAmt = (price * duty.effective) / 100;

    $("hl-loan").textContent = inr(la.loan);
    $("hl-loan-eyebrow").textContent = la.sanctioned ? "Your sanctioned loan" : "Bank can lend";
    $("hl-loan-note").textContent = la.sanctioned
      ? (la.overCap
          ? `${Math.round(la.ratio * 100)}% of the price — above RBI's ${Math.round((la.cap / price) * 100)}% cap for a loan ${la.label}; check the letter`
          : `${Math.round(la.ratio * 100)}% of the price — RBI's cap would have allowed ${compact(la.cap)}`)
      : `${Math.round(la.ratio * 100)}% of the price — RBI's cap for a loan ${la.label}`;
    $("hl-cash").textContent = inr(down + dutyAmt);
    $("hl-cash-note").textContent =
      `${compact(down)} down payment` + (la.sanctioned ? " (price − your loan)" : "") +
      ` + ${compact(dutyAmt)} stamp duty & registration (${duty.effective.toFixed(2)}%)` +
      (duty.verified ? "" : " — rate not recently verified");

    if (linked && buying()) {
      amountEl.value = Math.round(la.loan).toLocaleString("en-IN");
    }
    return la;
  }

  function setMode() {
    const buy = buying();
    $("hl-house").hidden = !buy;
    $("hl-loan-heading").textContent = buy ? "The loan" : "Your loan today";
    $("hl-years-label").textContent = buy ? "Tenure" : "Years left";
    amountEl.placeholder = buy ? "" : "what you still owe";
    if (buy) { linked = true; recalcHouse(); } else { linked = false; }
    syncAmountHint();
  }

  function syncAmountHint() {
    const buy = buying();
    $("hl-amount-hint").textContent = buy ? (linked ? "from the house above" : "edited") : "what you still owe";
    relinkEl.hidden = !(buy && !linked);
  }

  // --- the loan ------------------------------------------------------------------
  function recalcLoan() {
    const balance = num(amountEl, 0);
    const ratePct = num(rateEl, cfg.rate);
    const years = Math.max(1, num(yearsEl, cfg.years));
    const parked = num(parkedEl, 0), prepay = num(prepayEl, 0);
    const s = payoffScenarios(balance, ratePct, years, num(emiEl, 0), prepay, parked);
    emiEl.placeholder = Math.round(s.scheduledEmi).toLocaleString("en-IN");
    $("po-sched").textContent = inr(s.scheduledEmi);
    $("po-sched-note").textContent = `what the bank asks for over ${years.toFixed(0)} years`;

    const { base, combined } = s;
    const savedEl = $("po-saved");
    savedEl.classList.remove("loss", "gain");
    if (s.levers) savedEl.classList.add("gain");
    $("po-saved-eyebrow").textContent = s.levers ? "Interest you save" : "Total interest";
    if (balance <= 0 || !base.closes) {
      $("po-ends").textContent = "—";
      $("po-ends-note").textContent = "enter the loan amount, the rate and the years";
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
      } else if (s.levers) {
        savedEl.textContent = inr(saved);
        $("po-saved-note").textContent = `${compact(combined.interest)} interest instead of ${compact(base.interest)}`;
      } else {
        savedEl.textContent = inr(combined.interest);
        $("po-saved-note").textContent = `${compact(balance + combined.interest)} repaid in all — set a lever to see what you'd save`;
      }
    }

    // Income check against what actually goes out each month.
    const income = num(incomeEl, 0);
    const verdict = $("hl-verdict");
    const outgo = s.emiPaid + prepay;
    if (income > 0 && balance > 0) {
      const allowed = (income * cfg.foir) / 100;
      const share = (outgo / income) * 100;
      const ok = outgo <= allowed;
      verdict.hidden = false;
      verdict.className = "hl-verdict " + (ok ? "ok" : "no");
      verdict.textContent = ok
        ? `${inr(outgo)} a month is ${share.toFixed(0)}% of your income — inside the ~${cfg.foir}% most lenders allow.`
        : `${inr(outgo)} a month is ${share.toFixed(0)}% of your income. Lenders usually stop near ${cfg.foir}%, so expect to be offered less — around ${compact(allowed)} a month.`;
    } else {
      verdict.hidden = true;
    }

    // The lever table.
    $("po-rows").innerHTML = s.rows.map((r, i) => {
      const cls = (i === s.rows.length - 1 && s.levers > 1) ? ' class="hl-total"' : "";
      if (!r.closes) {
        return `<tr${cls}><td>${r.label}</td><td class="num loss" colspan="3">Never — the payment doesn't cover the interest</td></tr>`;
      }
      const savedCell = i === 0 ? '<td class="num gain">—</td>'
        : r.saved < -0.5 ? `<td class="num loss">−${inr(-r.saved)}</td>`
        : `<td class="num gain">${inr(r.saved)}</td>`;
      return `<tr${cls}><td>${r.label}</td><td class="num">${yrsMo(r.months)}</td>` +
             `<td class="num">${inr(r.interest)}</td>${savedCell}</tr>`;
    }).join("");

    // The ladder: extra per month on top of the EMI paid and what's parked.
    const rows = ladder(balance, ratePct, s.emiPaid, parked);
    $("hl-ladder").innerHTML = rows.length ? rows.map((r) =>
      `<tr><td>${r.base ? "Nothing" : inr(r.monthly) + `<span class="hl-pm">about ${inr(r.monthly * 12)} a year</span>`}</td>` +
      `<td class="num">${yrsMo(r.months)}</td><td class="num">${compact(r.interest)}</td>` +
      `<td class="num gain">${r.base ? "—" : compact(r.saved)}</td></tr>`).join("")
      : `<tr><td colspan="4" class="loss">That EMI doesn't cover the interest, so no amount of extra finishes it on this schedule.</td></tr>`;

    // The classic rule of thumb, priced: one extra EMI a year ≈ a twelfth a month.
    const thumb = $("hl-thumb");
    if (rows.length && combined.closes) {
      const extra = payoff(balance, ratePct, s.emiPaid, s.scheduledEmi / 12, parked);
      thumb.textContent = `One extra EMI a year (about ${inr(s.scheduledEmi / 12)} a month) ends it in ${(extra.months / 12).toFixed(1)} years and saves ${compact(rows[0].interest - extra.interest)}.`;
    } else {
      thumb.textContent = "";
    }

    // The schedule, as set above.
    const showNet = parked > 0;
    $("sched-net-head").hidden = !showNet;
    $("sched-rows").innerHTML = combined.yearly.map((y) =>
      `<tr><td>${y.year}</td><td class="num">${inr(y.opening)}</td><td class="num">${inr(y.paid)}</td>` +
      `<td class="num">${inr(y.interest)}</td><td class="num">${inr(y.principal)}</td>` +
      `<td class="num">${inr(y.closing)}</td>` + (showNet ? `<td class="num">${inr(y.net)}</td>` : "") + `</tr>`).join("")
      || `<tr><td colspan="7" class="loss">No schedule — the loan never closes as set.</td></tr>`;
    $("sched-note").textContent = showNet
      ? "Nominal rupees. “Owed at end” is the book balance; the last column nets off what's parked — the loan is closed when that reaches zero, and the parked money is yours again."
      : "Nominal rupees. A partial final year is shown as a year of its own.";
  }

  function recalcAll() { recalcHouse(); recalcLoan(); }

  [priceEl, sanctionEl].forEach((el) => el.addEventListener("input", recalcAll));
  stateEl.addEventListener("input", recalcHouse);
  stateEl.addEventListener("change", recalcHouse);
  amountEl.addEventListener("input", () => { linked = false; syncAmountHint(); recalcLoan(); });
  relinkEl.addEventListener("click", (e) => { e.preventDefault(); linked = true; syncAmountHint(); recalcAll(); });
  [rateEl, yearsEl, emiEl, prepayEl, parkedEl, incomeEl].forEach((el) => el && el.addEventListener("input", recalcLoan));
  [modeBuy, modeHave].forEach((el) => el && el.addEventListener("change", () => { setMode(); recalcLoan(); }));
  setMode();
  recalcLoan();
}
