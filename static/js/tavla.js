/* Tavlan: glidpanel, autospar, timer, dra-och-släpp, snabbtillägg, tangentbord.
   Servern är sanningen - varje handling är ett anrop och svaret säger vad som
   ska ritas om: {card, panel, stats}. Klienten gissar aldrig ett tillstånd.

   Kundmejl: den enda knappen som mejlar är "Svar + mejl till kunden"
   (data-act="comment-email"), och den bekräftar först. Inget annat här
   anropar mejlvägen. */
(function () {
  "use strict";
  var csrfEl = document.querySelector("#csrf [name=csrfmiddlewaretoken]");
  var csrf = csrfEl ? csrfEl.value : "";
  var board = document.getElementById("board");
  var drawer = document.getElementById("drawer");
  var scrim = document.getElementById("scrim");
  var openId = null;

  function post(url, payload) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(payload || {}),
    }).then(parse);
  }
  function postForm(url, formData) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Accept": "application/json", "X-CSRFToken": csrf }, body: formData,
    }).then(parse);
  }
  function parse(r) {
    return r.json().catch(function () { return { ok: false, error: "http " + r.status }; }).then(function (d) {
      if (!r.ok || !d.ok) { var err = new Error(d.error || ("http " + r.status)); err.data = d; throw err; }
      return d;
    });
  }
  function urlFor(name, id) { return board.getAttribute("data-" + name + "-url").replace("/0/", "/" + id + "/"); }
  function fmt(s) {
    s = Math.max(0, Math.floor(s)); var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    var mm = (m < 10 ? "0" : "") + m, ss = (x < 10 ? "0" : "") + x; return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
  }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  // --- besked ------------------------------------------------------------------
  var toastEl = null, toastT = null;
  function toast(text, isError) {
    if (!toastEl) { toastEl = document.createElement("div"); toastEl.className = "tv-toast"; toastEl.setAttribute("role", "status"); document.body.appendChild(toastEl); }
    toastEl.textContent = text; toastEl.classList.toggle("is-error", !!isError); toastEl.classList.add("is-on");
    clearTimeout(toastT); toastT = setTimeout(function () { toastEl.classList.remove("is-on"); }, isError ? 5000 : 2200);
  }
  function fail(err) { toast(err && err.message ? err.message : "Något gick fel.", true); }

  // --- bekräfta innan destruktiva formulär (även på projekt- och kundsidorna) ----
  document.addEventListener("submit", function (e) {
    var f = e.target.closest("form[data-confirm]");
    if (f && !window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-confirm][type=submit]");
    if (b && !window.confirm(b.getAttribute("data-confirm"))) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  // --- tickande klockor: varje [data-timer][data-live] räknar från sin bas ----------
  var loadedAt = Date.now();
  function tick() {
    document.querySelectorAll("[data-timer][data-live]").forEach(function (el) {
      var base = parseInt(el.getAttribute("data-seconds"), 10) || 0;
      var since = parseInt(el.getAttribute("data-since") || loadedAt, 10);
      el.textContent = fmt(base + (Date.now() - since) / 1000);
    });
  }
  setInterval(tick, 1000); tick();

  // --- huvudet: timerchip och siffror ritas om från stats -------------------------
  function renderTimerChip(timer) {
    var slot = document.getElementById("tv-timer"); if (!slot) return;
    if (timer) {
      slot.innerHTML = '<button type="button" class="tv-chip timer" data-act="stop-timer" data-issue="' + timer.issue_id + '" title="Stoppa timern"><span class="dot"></span><span>' + esc(timer.key) + '</span><b data-timer="' + timer.issue_id + '" data-seconds="' + timer.seconds + '" data-live="1" data-since="' + Date.now() + '">' + fmt(timer.seconds) + '</b><span>stopp</span></button>';
    } else {
      slot.innerHTML = '<span class="tv-chip timer idle"><span class="dot"></span><span>Ingen timer</span></span>';
    }
  }
  function applyStats(stats) {
    if (!stats) return;
    renderTimerChip(stats.timer);
    var el = document.getElementById("tv-stats"); if (!el) return;
    var html = '<span class="tv-chip"><span>I dag</span><b data-today>' + esc(stats.today) + '</b></span>';
    if (stats.late) html += '<a class="tv-chip hot" href="?forfaller=1"><b>' + stats.late + '</b> försenade</a>';
    if (stats.due) html += '<a class="tv-chip" href="?forfaller=1"><b>' + stats.due + '</b> förfaller i veckan</a>';
    el.innerHTML = html;
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest('[data-act="stop-timer"]'); if (!b) return;
    var stopUrl = board ? board.getAttribute("data-stop-url") : null;
    if (!stopUrl) {
      // Projekt-, kund- och tidsidan: inget tavelbräde att uppdatera, vanlig POST räcker.
      var f = document.createElement("form"); f.method = "post"; f.action = "/manage/tavla/timer/stopp/";
      f.innerHTML = '<input type="hidden" name="csrfmiddlewaretoken" value="' + csrf + '"><input type="hidden" name="next" value="' + esc(location.pathname + location.search) + '">';
      document.body.appendChild(f); f.submit(); return;
    }
    var id = b.getAttribute("data-issue");
    post(stopUrl, {}).then(function (d) {
      var card = board.querySelector('.tv-card[data-id="' + id + '"]');
      if (card) freezeCard(card);
      applyStats(d.stats);
      if (openId && String(openId) === String(id)) refreshPanel();
    }).catch(fail);
  });
  function freezeCard(card) {
    card.classList.remove("is-running");
    var t = card.querySelector(".tv-timer"); if (t) { t.classList.remove("is-on"); t.setAttribute("aria-pressed", "false"); }
    var v = card.querySelector("[data-timer]"); if (v) { v.setAttribute("data-seconds", v.textContent.split(":").reduce(function (a, b) { return a * 60 + parseInt(b, 10); }, 0)); v.removeAttribute("data-live"); v.removeAttribute("data-since"); }
  }

  if (!board) return;   // projekt-, kund- och tidsidan: bara det ovan
  var projectKey = board.getAttribute("data-project") || "";

  // --- svaret från servern: rita om kort, panel, huvud ----------------------------
  function applyCard(html, id) {
    var tmp = document.createElement("div"); tmp.innerHTML = html.trim();
    var fresh = tmp.firstElementChild; if (!fresh) return null;
    var old = board.querySelector('.tv-card[data-id="' + (id || fresh.getAttribute("data-id")) + '"]');
    if (old) {
      old.replaceWith(fresh);
    } else {
      // Nytt kort (snabbtillägg): hamnar i kolumnen som anroparen valt.
      return fresh;
    }
    if (fresh.querySelector("[data-live]")) fresh.querySelector("[data-live]").setAttribute("data-since", Date.now());
    if (String(fresh.getAttribute("data-id")) === String(openId)) fresh.classList.add("is-open");
    return fresh;
  }
  function apply(d, opts) {
    opts = opts || {};
    if (d.card) {
      var fresh = applyCard(d.card, opts.id);
      if (fresh && opts.col && !fresh.parentNode) { opts.col.querySelector(".tv-col-body").insertBefore(fresh, opts.col.querySelector("[data-empty]")); }
      // Kolumnbyte via panelen: kortet ska stå i rätt kolumn. På ett projekts
      // tavla heter kolumnen c<id>; på "Alla" heter den som steget (new/active/done).
      if (fresh && (opts.moveTo || d.stage)) {
        var col = (opts.moveTo && board.querySelector('.tv-col[data-col="' + opts.moveTo + '"]')) || (d.stage && board.querySelector('.tv-col[data-col="' + d.stage + '"]'));
        if (col && fresh.parentNode !== col.querySelector(".tv-col-body")) col.querySelector(".tv-col-body").insertBefore(fresh, col.querySelector("[data-empty]"));
      }
    }
    if (d.panel && drawer) mount(d.panel);
    applyStats(d.stats);
    refreshCounts(); tick();
  }
  function refreshCounts() {
    board.querySelectorAll(".tv-col").forEach(function (col) {
      var cards = col.querySelectorAll(".tv-card:not(.is-hidden)"), n = cards.length, el = col.querySelector("[data-count]");
      var wip = el ? el.getAttribute("data-wip") : "";
      if (el) el.textContent = n + (wip ? "/" + wip : "");
      col.classList.toggle("is-over", !!wip && n > parseInt(wip, 10));
      var t = 0; cards.forEach(function (c) { var x = c.querySelector("[data-timer]"); if (x) t += parseInt(x.getAttribute("data-seconds"), 10) || 0; });
      var te = col.querySelector("[data-coltime]"); if (te) { var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60); te.textContent = h + ":" + (m < 10 ? "0" : "") + m; }
      var empty = col.querySelector("[data-empty]"); if (empty) empty.hidden = n > 0;
    });
  }

  // --- glidpanelen -----------------------------------------------------------------
  function mount(html) {
    var y = drawer.scrollTop;
    drawer.innerHTML = html; drawer.classList.add("is-open"); drawer.classList.remove("is-loading"); drawer.setAttribute("aria-hidden", "false");
    scrim.classList.add("is-open"); drawer.scrollTop = y;
    var live = drawer.querySelector("[data-live]"); if (live) live.setAttribute("data-since", Date.now());
    if (window.AdxTiptap) window.AdxTiptap.init();  // beskrivningen blir en Tiptap-editor
    tick();
  }
  function open(id) {
    if (openId && String(openId) !== String(id)) markOpen(null);
    openId = id; markOpen(id); drawer.classList.add("is-loading");
    fetch(urlFor("panel", id), { credentials: "same-origin", headers: { "Accept": "application/json" } }).then(parse)
      .then(function (d) { if (String(openId) === String(id)) mount(d.panel); })
      .catch(function (err) { openId = null; markOpen(null); drawer.classList.remove("is-loading"); fail(err); });
    var u = new URL(location.href); u.searchParams.set("arende", id); history.replaceState(null, "", u);
  }
  function close() {
    openId = null; markOpen(null);
    drawer.classList.remove("is-open"); drawer.setAttribute("aria-hidden", "true"); scrim.classList.remove("is-open");
    var u = new URL(location.href); u.searchParams.delete("arende"); history.replaceState(null, "", u);
  }
  function refreshPanel() { if (openId) fetch(urlFor("panel", openId), { credentials: "same-origin", headers: { "Accept": "application/json" } }).then(parse).then(function (d) { if (drawer.classList.contains("is-open")) mount(d.panel); }).catch(function () {}); }
  function markOpen(id) { board.querySelectorAll(".tv-card.is-open").forEach(function (c) { c.classList.remove("is-open"); }); if (id) { var c = board.querySelector('.tv-card[data-id="' + id + '"]'); if (c) c.classList.add("is-open"); } }
  function flash(ok, text) {
    var s = drawer.querySelector("[data-saved]"); if (!s) return;
    s.textContent = text || (ok ? "Sparat" : "Kunde inte spara"); s.classList.toggle("is-error", !ok); s.classList.add("is-on");
    clearTimeout(flash.t); flash.t = setTimeout(function () { s.classList.remove("is-on", "is-error"); s.textContent = "Sparat"; }, ok ? 1400 : 4000);
  }
  scrim.addEventListener("click", close);

  // Autospar: ett fält, ett anrop. Panelen ritas bara om när fältet påverkar
  // annat i den (kolumn, etiketter, synlighet); annars bara kortet.
  function saveField(field, value, opts) {
    opts = opts || {};
    return post(urlFor("field", openId), { field: field, value: value, panel: !!opts.panel })
      .then(function (d) { apply(d, { moveTo: opts.moveTo }); flash(true); return d; })
      .catch(function (err) { flash(false, err.message); if (opts.panel) refreshPanel(); throw err; });
  }
  drawer.addEventListener("change", function (e) {
    var el = e.target, f = el.getAttribute("data-field"); if (!f) return;
    if (f === "column") { saveField("column", el.value, { panel: true, moveTo: el.value }).catch(function () {}); return; }
    if (el.tagName === "SELECT" || el.type === "date" || el.getAttribute("data-on") === "change") saveField(f, el.value).catch(function () {});
  });
  drawer.addEventListener("focusout", function (e) {
    var el = e.target, f = el.getAttribute && el.getAttribute("data-field"); if (!f) return;
    if (f === "title") { var v = el.textContent.trim(); if (!v) { refreshPanel(); return; } saveField("title", v).catch(function () {}); }
    if (f === "description") saveField("description", el.value).catch(function () {});
    var entryMin = el.getAttribute("data-entry-min");
    if (entryMin && el.value !== el.defaultValue) post(urlFor("time", entryMin), { minutes: el.value }).then(apply).then(function () { flash(true); }).catch(function (err) { flash(false, err.message); refreshPanel(); });
  });
  drawer.addEventListener("keydown", function (e) {
    var el = e.target;
    if (el.getAttribute("data-field") === "title" && e.key === "Enter") { e.preventDefault(); el.blur(); }
    if (el.getAttribute("data-entry-min") && e.key === "Enter") { e.preventDefault(); el.blur(); }
    if (e.key === "Escape" && el.matches("input,textarea,select,[contenteditable]")) { e.stopPropagation(); el.blur(); }
  });
  drawer.addEventListener("click", function (e) {
    var det = drawer.querySelector(".tv-detail"); if (!det) return;
    var sw = e.target.closest(".tv-switch[data-field]");
    if (sw) { var on = sw.getAttribute("aria-checked") !== "true"; sw.setAttribute("aria-checked", String(on)); saveField(sw.getAttribute("data-field"), on, { panel: true }).catch(function () {}); return; }
    var lab = e.target.closest('[data-field="label"]');
    if (lab) { lab.classList.toggle("is-on"); saveField("label", lab.getAttribute("data-value"), { panel: true }).catch(function () {}); return; }
    var del = e.target.closest("[data-check-delete]");
    if (del) { post(urlFor("check", del.getAttribute("data-check-delete")), { delete: true, panel: true }).then(apply).catch(fail); return; }
    var edel = e.target.closest("[data-entry-delete]");
    if (edel) { if (!window.confirm("Ta bort tidsposten?")) return; post(urlFor("time", edel.getAttribute("data-entry-delete")), { delete: true }).then(apply).catch(fail); return; }
    var a = e.target.closest("[data-act]"); if (!a) return;
    var act = a.getAttribute("data-act");
    if (act === "close") close();
    else if (act === "timer") toggleTimer(openId, a, true);
    else if (act === "move") moveNext(board.querySelector('.tv-card[data-id="' + openId + '"]'), true);
    else if (act === "comment-internal" || act === "comment-portal" || act === "comment-email") {
      var ta = det.querySelector("[data-comment]"), body = ta.value.trim();
      if (!body) { ta.focus(); return; }
      if (act === "comment-email" && !window.confirm(a.getAttribute("data-confirm") || "Mejla kunden?")) return;
      a.classList.add("is-busy");
      var req = act === "comment-email"
        ? post(urlFor("email", openId), { body: body })
        : post(urlFor("comment", openId), { body: body, internal: act === "comment-internal" });
      req.then(function (d) {
        apply(d);
        if (act === "comment-email") toast(d.mailed ? "Mejlet är skickat till kunden." : (d.error || "Sparat, men mejlet gick inte iväg."), !d.mailed);
        else toast(act === "comment-internal" ? "Intern anteckning sparad." : "Svaret syns i portalen. Inget mejl har skickats.");
      }).catch(fail).then(function () { a.classList.remove("is-busy"); });
    }
  });
  drawer.addEventListener("change", function (e) {
    var chk = e.target.getAttribute && e.target.getAttribute("data-check");
    if (chk) post(urlFor("check", chk), { done: e.target.checked, panel: true }).then(apply).catch(fail);
    var files = e.target.closest && e.target.closest('.tv-upload input[type=file]');
    if (files && files.files.length) {
      var fd = new FormData(); Array.prototype.forEach.call(files.files, function (f) { fd.append("files", f); }); fd.append("panel", "1");
      drawer.classList.add("is-loading");
      postForm(urlFor("attach", openId), fd).then(apply).then(function () { toast("Uppladdat."); }).catch(function (err) { drawer.classList.remove("is-loading"); fail(err); });
    }
  });
  drawer.addEventListener("submit", function (e) {
    var f = e.target; e.preventDefault();
    var act = f.getAttribute("data-act");
    if (act === "add-check") { var t = f.text.value.trim(); if (!t) return; post(urlFor("check-add", openId), { text: t }).then(function (d) { apply(d); var i = drawer.querySelector('[data-act="add-check"] input'); if (i) i.focus(); }).catch(fail); }
    if (act === "add-time") { post(urlFor("time-add", openId), { minutes: f.minutes.value, date: f.date.value, note: f.note.value }).then(function (d) { apply(d); toast("Tid loggad."); }).catch(fail); }
  });

  // --- kort: timer, flytta, öppna --------------------------------------------------------
  function toggleTimer(id, btn, panel) {
    return post(urlFor("timer", id), { panel: !!panel }).then(function (d) {
      (d.stopped || []).forEach(function (oid) { var other = board.querySelector('.tv-card[data-id="' + oid + '"]'); if (other) freezeCard(other); });
      apply(d);
    }).catch(fail);
  }
  function colOf(el) { return el.closest(".tv-col"); }
  function idsIn(col) { return Array.prototype.map.call(col.querySelectorAll(".tv-card"), function (c) { return parseInt(c.getAttribute("data-id"), 10); }); }
  function moveTo(card, col, beforeEl, panel) {
    if (!card || !col) return Promise.resolve();
    col.querySelector(".tv-col-body").insertBefore(card, beforeEl || col.querySelector("[data-empty]"));
    refreshCounts();
    var id = parseInt(card.getAttribute("data-id"), 10);
    return post(urlFor("move", id), { target: col.getAttribute("data-col"), order: idsIn(col), panel: !!panel })
      .then(apply).catch(function (err) { fail(err); window.setTimeout(function () { window.location.reload(); }, 1200); });
  }
  function nextCol(col) { var cols = Array.prototype.slice.call(board.querySelectorAll(".tv-col")); return cols[(cols.indexOf(col) + 1) % cols.length]; }
  function moveNext(card, panel) { if (card) moveTo(card, nextCol(colOf(card)), null, panel); }

  board.addEventListener("click", function (e) {
    var card = e.target.closest(".tv-card"); if (!card) return;
    var a = e.target.closest("[data-act]");
    if (a && a.getAttribute("data-act") === "timer") { e.preventDefault(); toggleTimer(card.getAttribute("data-id"), a, String(openId) === card.getAttribute("data-id")); }
    else if (a && a.getAttribute("data-act") === "move") { e.preventDefault(); moveNext(card, String(openId) === card.getAttribute("data-id")); }
    else open(card.getAttribute("data-id"));
  });

  // --- snabbtillägg --------------------------------------------------------------------
  board.addEventListener("submit", function (e) {
    var form = e.target.closest(".tv-add"); if (!form) return;
    e.preventDefault();
    var title = form.title.value.trim(); if (!title) return;
    post(board.getAttribute("data-add-url"), { title: title, target: form.getAttribute("data-col"), project: projectKey }).then(function (d) {
      apply(d, { col: colOf(form), id: d.id });
      form.title.value = ""; form.title.focus();
    }).catch(fail);
  });

  // --- sök: omedelbart i klienten, adressen följer med -------------------------------------
  var search = document.querySelector(".tv-search");
  if (search) {
    var searchT = null;
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase();
      board.querySelectorAll(".tv-card").forEach(function (c) {
        var hay = (c.getAttribute("aria-label") + " " + c.querySelector(".tv-meta").textContent).toLowerCase();
        c.classList.toggle("is-hidden", !!q && hay.indexOf(q) < 0);
      });
      refreshCounts();
      clearTimeout(searchT); searchT = setTimeout(function () { var u = new URL(location.href); if (q) u.searchParams.set("q", search.value.trim()); else u.searchParams.delete("q"); history.replaceState(null, "", u); }, 300);
    });
    search.addEventListener("keydown", function (e) { if (e.key === "Escape") { search.value = ""; search.dispatchEvent(new Event("input")); search.blur(); } });
  }

  // --- tangentbord --------------------------------------------------------------------------
  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target;
    if (t.matches && t.matches("input,textarea,select,[contenteditable]")) return;
    if (e.key === "/") { e.preventDefault(); if (search) search.focus(); return; }
    if (e.key === "Escape" && openId) { close(); return; }
    if (e.key.toLowerCase() === "n") { var i = board.querySelector(".tv-add input"); if (i) { e.preventDefault(); i.focus(); } return; }
    var card = t.closest && t.closest(".tv-card"); if (!card) return;
    var cols = Array.prototype.slice.call(board.querySelectorAll(".tv-col"));
    if (/^[1-9]$/.test(e.key) && cols[+e.key - 1]) { e.preventDefault(); moveTo(card, cols[+e.key - 1], null, String(openId) === card.getAttribute("data-id")); }
    if (e.key === "Enter") { e.preventDefault(); open(card.getAttribute("data-id")); }
    if (e.key === " ") { e.preventDefault(); toggleTimer(card.getAttribute("data-id"), null, String(openId) === card.getAttribute("data-id")); }
    if (e.key === "ArrowRight") { e.preventDefault(); moveNext(card, false); }
  });

  // --- dra och släpp (pointer events; touch efter lång tryckning) -------------------------------
  var drag = null;
  board.addEventListener("pointerdown", function (e) {
    var card = e.target.closest(".tv-card"); if (!card || e.target.closest("button, a")) return;
    drag = { x: e.clientX, y: e.clientY, card: card, touch: e.pointerType === "touch", armed: e.pointerType !== "touch", ghost: null };
    if (drag.touch) drag.hold = setTimeout(function () { drag.armed = true; card.classList.add("is-armed"); if (navigator.vibrate) navigator.vibrate(15); }, 260);
  });
  document.addEventListener("pointermove", function (e) {
    if (!drag) return; var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (!drag.ghost) {
      if (!drag.armed) { if (Math.abs(dx) + Math.abs(dy) > 10) { clearTimeout(drag.hold); drag = null; } return; }
      if (Math.abs(dx) + Math.abs(dy) < 6) return;
      var r = drag.card.getBoundingClientRect(); drag.ghost = drag.card.cloneNode(true); drag.ghost.className += " is-ghost";
      drag.ghost.style.cssText = "position:fixed;pointer-events:none;z-index:999;width:" + r.width + "px;left:" + r.left + "px;top:" + r.top + "px;margin:0;";
      drag.offX = e.clientX - r.left; drag.offY = e.clientY - r.top; document.body.appendChild(drag.ghost);
      drag.card.classList.add("is-dragging"); document.body.classList.add("is-dragging-any");
    }
    e.preventDefault(); drag.ghost.style.left = (e.clientX - drag.offX) + "px"; drag.ghost.style.top = (e.clientY - drag.offY) + "px";
    var under = document.elementFromPoint(e.clientX, e.clientY), col = under && under.closest(".tv-col");
    board.querySelectorAll(".tv-col.is-target").forEach(function (c) { c.classList.remove("is-target"); }); if (col) col.classList.add("is-target");
  }, { passive: false });
  document.addEventListener("pointerup", function (e) {
    if (!drag) return; clearTimeout(drag.hold);
    var moved = !!drag.ghost;
    if (drag.ghost) {
      var under = document.elementFromPoint(e.clientX, e.clientY), col = under && under.closest(".tv-col");
      drag.ghost.remove();
      if (col) {
        var before = null;
        col.querySelectorAll(".tv-card:not(.is-dragging)").forEach(function (c) { var r = c.getBoundingClientRect(); if (!before && e.clientY < r.top + r.height / 2) before = c; });
        moveTo(drag.card, col, before, String(openId) === drag.card.getAttribute("data-id"));
      }
    }
    board.querySelectorAll(".is-target,.is-dragging,.is-armed").forEach(function (c) { c.classList.remove("is-target", "is-dragging", "is-armed"); });
    document.body.classList.remove("is-dragging-any"); drag = null;
    // Ett drag ska inte också räknas som ett klick som öppnar panelen.
    if (moved) { var stop = function (ev) { ev.stopPropagation(); ev.preventDefault(); board.removeEventListener("click", stop, true); }; board.addEventListener("click", stop, true); setTimeout(function () { board.removeEventListener("click", stop, true); }, 0); }
  });
  document.addEventListener("pointercancel", function () { if (drag) { clearTimeout(drag.hold); if (drag.ghost) drag.ghost.remove(); drag = null; } });

  // --- djuplänk: ?arende=<id> öppnar panelen direkt --------------------------------------------
  var initial = board.getAttribute("data-open");
  if (initial) open(initial);
})();
