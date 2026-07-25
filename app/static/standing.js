// "Where do you stand?" — rank a net worth across geographies, live.
//
// Dependency-free and fully local: the distribution constants are embedded by the
// server (#stand-data) and every number is computed in the browser, so nothing a
// visitor types ever leaves the page. The ranking mirrors app/wealth.py exactly —
// a piecewise power-law (Pareto) interpolation between the known band edges — which
// is the tested, canonical source; keep the two in sync if either changes.

(function () {
  "use strict";

  var DATA = null; // distribution dataset from the server
  var CRORE = 1e7;
  var currentINR = 0;
  var selectedGeo = "india"; // which geography the pyramid shows

  // --- Ranking (mirror of app/wealth.py) ------------------------------------

  function anchors(geo) {
    // (boundary_inr, adults_at_or_above) ascending in wealth.
    var counts = DATA.bandCounts[geo];
    var edges = DATA.bandEdgesCr;
    var out = [];
    for (var e = 0; e < edges.length; e++) {
      var n = 0;
      for (var k = e + 1; k < counts.length; k++) n += counts[k];
      out.push([edges[e] * CRORE, n]);
    }
    return out;
  }

  function alpha(w1, n1, w2, n2) {
    return -Math.log(n2 / n1) / Math.log(w2 / w1);
  }

  function headCount(inr, anc, pop) {
    if (inr <= 0) return pop;
    var loW = anc[0][0];
    var hiW = anc[anc.length - 1][0];
    var hiN = anc[anc.length - 1][1];
    if (inr <= loW) {
      var a0 = alpha(anc[0][0], anc[0][1], anc[1][0], anc[1][1]);
      return Math.min(pop, anc[0][1] * Math.pow(inr / anc[0][0], -a0));
    }
    if (inr >= hiW) {
      var n = anc.length;
      var aN = alpha(anc[n - 2][0], anc[n - 2][1], anc[n - 1][0], anc[n - 1][1]);
      return Math.max(0, hiN * Math.pow(inr / hiW, -aN));
    }
    for (var i = 0; i < anc.length - 1; i++) {
      var w1 = anc[i][0], n1 = anc[i][1], w2 = anc[i + 1][0], n2 = anc[i + 1][1];
      if (inr >= w1 && inr <= w2) {
        return n1 * Math.pow(inr / w1, -alpha(w1, n1, w2, n2));
      }
    }
    return pop;
  }

  function bandIndex(inr) {
    var cr = inr / CRORE;
    var edges = DATA.bandEdgesCr;
    for (var i = 0; i < edges.length; i++) if (cr < edges[i]) return i;
    return edges.length;
  }

  function place(inr, geo) {
    var meta = DATA.geoMeta[geo];
    var pop = meta.adults;
    var n = Math.max(0, Math.min(pop, headCount(inr, anchors(geo), pop)));
    var topPct = (n / pop) * 100;
    var bi = bandIndex(inr);
    return {
      geo: geo, name: meta.name, flag: meta.flag, adults: pop,
      topPct: topPct, richerThanPct: 100 - topPct, rank: n,
      oneIn: n > 0 ? pop / n : null,
      bandIndex: bi, bandLabel: DATA.bandLabels[bi],
    };
  }

  function rankAll(inr) {
    return DATA.geoOrder.map(function (g) { return place(inr, g); });
  }

  // --- Formatting -----------------------------------------------------------

  function shortNum(x) {
    if (x >= 100) return Math.round(x).toLocaleString("en-IN");
    if (x >= 10) return x.toFixed(1).replace(/\.0$/, "");
    return x.toFixed(2).replace(/\.?0+$/, "");
  }

  // Human-readable count for an Indian audience (lakh / crore words).
  function humanIN(n) {
    n = Math.round(n);
    if (n < 1) return "less than 1";
    if (n < 1e5) return n.toLocaleString("en-IN");
    if (n < 1e7) return shortNum(n / 1e5) + " lakh";
    return shortNum(n / 1e7) + " crore";
  }

  function fmtPct(p) {
    if (p <= 0) return "0";
    if (p >= 1) return p.toFixed(1);
    if (p >= 0.1) return p.toFixed(2);
    if (p >= 0.01) return p.toFixed(3);
    if (p >= 0.001) return p.toFixed(4);
    if (p >= 0.0001) return p.toFixed(5);
    return "<0.0001";
  }

  function fmtINR(inr) {
    if (inr >= 1e7) return "₹" + shortNum(inr / 1e7) + " crore";
    if (inr >= 1e5) return "₹" + shortNum(inr / 1e5) + " lakh";
    return "₹" + Math.round(inr).toLocaleString("en-IN");
  }

  // Position on the shared log-percentile axis: top 100% (common) -> 0,
  // top 0.0001% (rare) -> 1. More exclusive sits further right.
  function axisPos(topPct) {
    if (topPct <= 0) return 1;
    var v = (2 - Math.log10(topPct)) / 6;
    return Math.max(0, Math.min(1, v));
  }

  // --- Rendering ------------------------------------------------------------

  var rowEls = {}; // geo -> row element, built once then updated in place

  function buildRows() {
    var host = document.getElementById("country-rows");
    host.innerHTML = "";

    // Shared scale header aligned to the track column.
    var scale = document.createElement("div");
    scale.className = "crow scale-row";
    var ticks = [50, 10, 1, 0.1, 0.01]
      .map(function (p) {
        var left = axisPos(p) * 100;
        var lbl = p >= 1 ? p + "%" : p + "%";
        return (
          '<span class="tick" style="left:' + left + '%">' +
          '<i></i><em>' + lbl + "</em></span>"
        );
      })
      .join("");
    scale.innerHTML =
      '<span class="crow-geo scale-cap">common</span>' +
      '<span class="crow-track scale-ticks">' + ticks + "</span>" +
      '<span class="crow-stat scale-cap right">rare →</span>';
    host.appendChild(scale);

    DATA.geoOrder.forEach(function (geo) {
      var meta = DATA.geoMeta[geo];
      var row = document.createElement("button");
      row.type = "button";
      row.className = "crow crow-btn";
      row.dataset.geo = geo;
      row.innerHTML =
        '<span class="crow-geo">' +
        '<span class="crank"></span>' +
        '<span class="flag">' + meta.flag + "</span>" +
        '<span class="cname">' + meta.name + "</span>" +
        "</span>" +
        '<span class="crow-track">' +
        '<span class="track-fill"></span>' +
        '<span class="track-marker"></span>' +
        "</span>" +
        '<span class="crow-stat">' +
        '<span class="top-pct"></span>' +
        '<span class="one-in muted"></span>' +
        "</span>";
      row.addEventListener("click", function () {
        selectGeo(geo);
      });
      host.appendChild(row);
      rowEls[geo] = row;
    });
  }

  function renderRows(inr) {
    var placements = rankAll(inr);

    // Exclusivity ranking (#1 = rarest) for the little rank badges.
    var byExcl = placements.slice().sort(function (a, b) {
      return a.topPct - b.topPct;
    });
    var exclRank = {};
    byExcl.forEach(function (p, i) { exclRank[p.geo] = i + 1; });

    placements.forEach(function (p) {
      var row = rowEls[p.geo];
      var pos = axisPos(p.topPct) * 100;
      row.querySelector(".track-fill").style.width = pos + "%";
      row.querySelector(".track-marker").style.left = pos + "%";
      row.querySelector(".top-pct").textContent = "Top " + fmtPct(p.topPct) + "%";

      var sub;
      if (p.rank < 1) {
        sub = "richer than almost everyone";
      } else {
        sub = "1 in " + humanIN(p.oneIn) + " adults";
      }
      row.querySelector(".one-in").textContent = sub;

      var badge = row.querySelector(".crank");
      badge.textContent = "#" + exclRank[p.geo];
      row.classList.toggle("is-top", exclRank[p.geo] === 1);
    });

    renderFunFact(placements);
  }

  function renderFunFact(placements) {
    var el = document.getElementById("fun-fact");
    var byGeo = {};
    placements.forEach(function (p) { byGeo[p.geo] = p; });
    var india = byGeo.india, usa = byGeo.usa;
    if (!india || !usa) { el.hidden = true; return; }
    // The headline contrast: the same money, two very different standings.
    var ratio = usa.topPct > 0 ? india.topPct / usa.topPct : 0;
    if (india.topPct < usa.topPct && ratio < 0.9 && india.rank >= 1) {
      el.innerHTML =
        "💡 The same " + fmtINR(currentINR) + " puts you in the <strong>top " +
        fmtPct(india.topPct) + "%</strong> in India — but only the <strong>top " +
        fmtPct(usa.topPct) + "%</strong> in the USA.";
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  }

  // --- Pyramid --------------------------------------------------------------

  function buildTabs() {
    var host = document.getElementById("pyramid-tabs");
    host.innerHTML = "";
    DATA.geoOrder.forEach(function (geo) {
      var meta = DATA.geoMeta[geo];
      var b = document.createElement("button");
      b.type = "button";
      b.className = "ptab";
      b.dataset.geo = geo;
      b.textContent = meta.flag + " " + meta.name;
      b.addEventListener("click", function () { selectGeo(geo); });
      host.appendChild(b);
    });
  }

  function renderPyramid(inr) {
    var geo = selectedGeo;
    var counts = DATA.bandCounts[geo];
    var meta = DATA.geoMeta[geo];
    var userBand = bandIndex(inr);
    document.getElementById("pyramid-geo-name").textContent = meta.name;

    // Active tab.
    Array.prototype.forEach.call(
      document.querySelectorAll("#pyramid-tabs .ptab"),
      function (b) { b.classList.toggle("active", b.dataset.geo === geo); }
    );

    // Log-scaled widths so the tiny top bands stay visible.
    var maxLog = Math.log10(Math.max.apply(null, counts) + 1);
    var host = document.getElementById("pyramid");
    host.innerHTML = "";

    // Top band (rarest) first, so the pyramid narrows upward on screen.
    for (var i = counts.length - 1; i >= 0; i--) {
      var c = counts[i];
      var w = 12 + (Math.log10(c + 1) / maxLog) * 88; // 12%..100%
      var shade = 0.1 + (i / (counts.length - 1)) * 0.8; // deeper = rarer
      var share = (c / meta.adults) * 100;
      var isUser = i === userBand;

      var rowWrap = document.createElement("div");
      rowWrap.className = "pyr-row" + (isUser ? " pyr-user" : "");

      var bar = document.createElement("div");
      bar.className = "pyr-bar";
      bar.style.width = w + "%";
      bar.style.setProperty("--shade", isUser ? 1 : shade);
      bar.innerHTML =
        '<span class="pyr-band">' + DATA.bandLabels[i] + "</span>" +
        '<span class="pyr-usd">' + DATA.bandUsdLabels[i] + "</span>";

      var meta2 = document.createElement("div");
      meta2.className = "pyr-meta";
      meta2.innerHTML =
        '<span class="pyr-count">' + humanIN(c) + " adults</span>" +
        '<span class="pyr-share muted">' + fmtPct(share) + "%</span>" +
        (isUser ? '<span class="pyr-you">◀ you\'re here</span>' : "");

      rowWrap.appendChild(bar);
      rowWrap.appendChild(meta2);
      host.appendChild(rowWrap);
    }
  }

  function selectGeo(geo) {
    selectedGeo = geo;
    Array.prototype.forEach.call(
      document.querySelectorAll(".crow-btn"),
      function (r) { r.classList.toggle("selected", r.dataset.geo === geo); }
    );
    renderPyramid(currentINR);
  }

  // --- Input wiring ---------------------------------------------------------

  var input, unitSel, slider;

  // Slider maps 0..1000 -> ₹1 lakh (1e5) .. ₹10,000 cr (1e11), log scale.
  var SLIDER_LO = 5, SLIDER_HI = 11;
  function sliderToINR(v) {
    var t = v / 1000;
    return Math.pow(10, SLIDER_LO + t * (SLIDER_HI - SLIDER_LO));
  }
  function inrToSlider(inr) {
    if (inr <= 0) return 0;
    var t = (Math.log10(inr) - SLIDER_LO) / (SLIDER_HI - SLIDER_LO);
    return Math.max(0, Math.min(1000, Math.round(t * 1000)));
  }

  function syncInputField(inr) {
    var exp = parseInt(unitSel.value, 10); // 7 = crore, 5 = lakh
    var val = inr / Math.pow(10, exp);
    input.value = val.toLocaleString("en-IN", { maximumFractionDigits: 2 });
  }

  function readInputField() {
    var raw = (input.value || "").replace(/,/g, "").trim();
    var val = parseFloat(raw);
    if (isNaN(val) || val < 0) return null;
    var exp = parseInt(unitSel.value, 10);
    return val * Math.pow(10, exp);
  }

  // Update everything. `source` avoids clobbering the field the user is typing in.
  function update(inr, source) {
    currentINR = inr;
    document.getElementById("lead-amount").textContent = fmtINR(inr);
    if (source !== "input") syncInputField(inr);
    if (source !== "slider") slider.value = inrToSlider(inr);
    renderRows(inr);
    renderPyramid(inr);
  }

  function initStanding(config) {
    DATA = JSON.parse(document.getElementById("stand-data").textContent);
    CRORE = DATA.crore;
    // Default the pyramid to the geography its own data leads with.
    selectedGeo = DATA.geoOrder[0];

    input = document.getElementById("nw-input");
    unitSel = document.getElementById("nw-unit");
    slider = document.getElementById("nw-slider");

    buildRows();
    buildTabs();

    input.addEventListener("input", function () {
      var inr = readInputField();
      if (inr !== null) update(inr, "input");
    });
    unitSel.addEventListener("change", function () {
      syncInputField(currentINR); // re-express the same money in the new unit
    });
    slider.addEventListener("input", function () {
      update(sliderToINR(parseFloat(slider.value)), "slider");
    });

    document.getElementById("nw-presets").addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-inr]");
      if (!btn) return;
      update(parseFloat(btn.dataset.inr), "preset");
    });

    selectGeo(selectedGeo);
    update(config.defaultNetWorth || 5e7, "init");
  }

  window.initStanding = initStanding;
})();
