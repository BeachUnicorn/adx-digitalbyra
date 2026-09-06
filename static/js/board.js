/* Tavlan: dra-och-släpp, timer, snabbtillägg, tangentbord. Servern är
   sanningen - varje handling är ett anrop, DOM:en uppdateras på svaret. */
(function () {
  "use strict";
  var csrfEl = document.querySelector("#csrf [name=csrfmiddlewaretoken]");
  var csrf = csrfEl ? csrfEl.value : "";

  function post(url, payload) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify(payload || {}),
    }).then(function (r) { if (!r.ok) throw new Error("http " + r.status); return r.json(); });
  }
  function fmt(s) {
    s = Math.max(0, Math.floor(s)); var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    var mm = (m < 10 ? "0" : "") + m, ss = (x < 10 ? "0" : "") + x; return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
  }

  // --- tickande klockor ------------------------------------------------------
  var loadedAt = Date.now();
  function tick() {
    document.querySelectorAll(".is-running [data-timer], .timer.is-on [data-timer]").forEach(function (el) {
      var base = parseInt(el.getAttribute("data-seconds"), 10) || 0;
      var since = parseInt(el.getAttribute("data-since") || loadedAt, 10);
      el.textContent = fmt(base + (Date.now() - since) / 1000);
    });
    var head = document.querySelector("[data-header-timer]");
    if (head) {
      var started = parseFloat(head.getAttribute("data-started")) * 1000;
      var base2 = parseInt(head.getAttribute("data-base"), 10) || 0;
      head.textContent = fmt(base2 + (Date.now() - loadedAt) / 1000);
      void started;
    }
  }
  setInterval(tick, 1000); tick();

  var board = document.getElementById("board");
  if (!board || !board.getAttribute("data-move-url")) return;   // portalen: ingen interaktion
  var moveUrl = board.getAttribute("data-move-url"), reorderUrl = board.getAttribute("data-reorder-url"),
      addUrl = board.getAttribute("data-add-url"), projectKey = board.getAttribute("data-project") || "";

  function colOf(el) { return el.closest(".col"); }
  function idsIn(col) { return Array.prototype.map.call(col.querySelectorAll(".card"), function (c) { return parseInt(c.getAttribute("data-id"), 10); }); }
  function refreshCounts() {
    board.querySelectorAll(".col").forEach(function (col) {
      var n = col.querySelectorAll(".card").length, el = col.querySelector("[data-count]");
      if (el) { var lim = (el.textContent.split("/")[1] || "").trim(); el.textContent = n + (lim ? "/" + lim : ""); }
      var t = 0; col.querySelectorAll("[data-timer]").forEach(function (x) { t += parseInt(x.getAttribute("data-seconds"), 10) || 0; });
      var te = col.querySelector("[data-coltime]"); if (te) te.textContent = fmt(t);
    });
  }
  function setHeader(title, running) {
    var h = document.getElementById("header-timer"); if (!h) return;
    var t = h.querySelector("[data-active-title]"); if (t) t.textContent = title || "Ingen timer igång";
    h.classList.toggle("is-idle", !running);
    if (running && h.tagName !== "FORM") { window.location.reload(); }   // stoppknappen finns bara server-renderad
  }

  function moveTo(card, col, beforeEl) {
    col.querySelector(".col-body").insertBefore(card, beforeEl || null);
    var id = parseInt(card.getAttribute("data-id"), 10);
    return post(moveUrl.replace("/0/", "/" + id + "/"), { target: col.getAttribute("data-col"), order: idsIn(col) })
      .then(function (d) { card.setAttribute("data-stage", d.stage); refreshCounts(); })
      .catch(function () { window.location.reload(); });
  }
  function nextCol(col) { var cols = Array.prototype.slice.call(board.querySelectorAll(".col")); return cols[(cols.indexOf(col) + 1) % cols.length]; }

  // --- klick: timer, flytta ----------------------------------------------------
  board.addEventListener("click", function (e) {
    var card = e.target.closest(".card"); if (!card) return;
    var timer = e.target.closest(".timer");
    if (timer) {
      e.preventDefault();
      post(timer.getAttribute("data-timer-url"), {}).then(function (d) {
        (d.stopped || []).forEach(function (oid) {
          var other = board.querySelector('.card[data-id="' + oid + '"]'); if (!other) return;
          other.classList.remove("is-running"); var ot = other.querySelector(".timer"); ot.classList.remove("is-on"); ot.setAttribute("aria-pressed", "false");
          var ov = other.querySelector("[data-timer]"); ov.setAttribute("data-seconds", ov.textContent.split(":").reduce(function (a, b) { return a * 60 + parseInt(b, 10); }, 0)); ov.removeAttribute("data-since");
        });
        var val = card.querySelector("[data-timer]");
        val.setAttribute("data-seconds", d.seconds); val.setAttribute("data-since", Date.now()); val.textContent = fmt(d.seconds);
        card.classList.toggle("is-running", d.running); timer.classList.toggle("is-on", d.running); timer.setAttribute("aria-pressed", String(d.running));
        refreshCounts(); setHeader(d.running ? d.title : "", d.running);
      }).catch(function () { window.location.reload(); });
    } else if (e.target.closest(".card-move")) {
      e.preventDefault(); moveTo(card, nextCol(colOf(card)), null);
    }
  });

  // --- snabbtillägg --------------------------------------------------------------
  board.addEventListener("submit", function (e) {
    var form = e.target.closest(".col-add"); if (!form) return;
    e.preventDefault();
    var title = form.title.value.trim(); if (!title) return;
    post(addUrl, { title: title, target: form.getAttribute("data-col"), project: projectKey }).then(function (d) {
      var col = colOf(form); col.querySelector(".col-body").insertAdjacentHTML("beforeend", d.html);
      form.title.value = ""; form.title.focus(); refreshCounts();
    }).catch(function () { window.location.reload(); });
  });

  // --- tangentbord -------------------------------------------------------------------
  board.addEventListener("keydown", function (e) {
    var card = e.target.closest && e.target.closest(".card"); if (!card || e.target !== card) return;
    if (e.key === " ") { e.preventDefault(); card.querySelector(".timer").click(); }
    if (e.key === "ArrowRight") { e.preventDefault(); moveTo(card, nextCol(colOf(card)), null); }
  });

  // --- dra och släpp (pointer events; touch efter lång tryckning) -------------------------
  var drag = null;
  board.addEventListener("pointerdown", function (e) {
    var card = e.target.closest(".card"); if (!card || e.target.closest("button, a")) return;
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
    var under = document.elementFromPoint(e.clientX, e.clientY), col = under && under.closest(".col");
    board.querySelectorAll(".col.is-target").forEach(function (c) { c.classList.remove("is-target"); }); if (col) col.classList.add("is-target");
  }, { passive: false });
  document.addEventListener("pointerup", function (e) {
    if (!drag) return; clearTimeout(drag.hold);
    if (drag.ghost) {
      var under = document.elementFromPoint(e.clientX, e.clientY), col = under && under.closest(".col");
      drag.ghost.remove();
      if (col) {
        var before = null;
        col.querySelectorAll(".card:not(.is-dragging)").forEach(function (c) { var r = c.getBoundingClientRect(); if (!before && e.clientY < r.top + r.height / 2) before = c; });
        moveTo(drag.card, col, before);
      }
    }
    board.querySelectorAll(".is-target,.is-dragging,.is-armed").forEach(function (c) { c.classList.remove("is-target", "is-dragging", "is-armed"); });
    document.body.classList.remove("is-dragging-any"); drag = null;
  });
  document.addEventListener("pointercancel", function () { if (drag) { clearTimeout(drag.hold); if (drag.ghost) drag.ghost.remove(); drag = null; } });
})();
