/* ==========================================================================
   ADX Flamingo, importen av kontakter (templates/flamingo/app/kontakter/import/).

   Allt här är valfritt; sidorna fungerar utan skript:
   1. Väntan: [data-im-poll="<url>?status=json"] frågar efter läget var
      tredje sekund, visar raderna i [data-im-progress-text] och
      <progress data-im-progress>, och går till jobbets sida när ticken är
      klar med steget (waiting blir false).
   2. Inklistringen: <textarea data-im-count="id" data-max="2000"> visar
      antalet rader (rubriken oräknad) i elementet med id:t.
   3. Steg 3: rutan [data-im-proof] (kanaler och "Var och när?") döljs när
      "Vet inte" är valt.
   4. Steg 4: fältet [data-im-newlist] syns bara när listan "Ny lista" är vald.
   5. Formulär med data-im-busy: knappen låses och visar texten när
      formuläret skickas (en fil eller en stor import tar en stund).
   ========================================================================== */
(function () {
  "use strict";

  var POLL_MS = 3000;
  var POLL_MAX_MS = 30000;

  function group(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function setupPoll(box) {
    var url = box.getAttribute("data-im-poll");
    var text = box.querySelector("[data-im-progress-text]");
    var bar = box.querySelector("[data-im-progress]");
    var delay = POLL_MS;
    var done = false;

    function schedule() {
      window.setTimeout(check, delay);
    }

    function check() {
      if (done) {
        return;
      }
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("status " + response.status);
          }
          return response.json();
        })
        .then(function (data) {
          if (!data.waiting) {
            done = true;
            window.location.assign(data.url);
            return;
          }
          if (text && data.text) {
            text.textContent = data.text;
          }
          if (bar && data.total) {
            bar.max = data.total;
            bar.value = Math.min(data.progress, data.total);
          }
          delay = POLL_MS;
          schedule();
        })
        .catch(function () {
          delay = Math.min(delay * 2, POLL_MAX_MS);
          schedule();
        });
    }

    schedule();
  }

  function setupCount(field) {
    var out = document.getElementById(field.getAttribute("data-im-count"));
    if (!out) {
      return;
    }
    var max = parseInt(field.getAttribute("data-max") || "0", 10);
    function update() {
      var lines = field.value.split(/\r\n|\r|\n/).filter(function (line) {
        return line.trim() !== "";
      });
      var rows = Math.max(0, lines.length - 1);
      if (!lines.length) {
        out.textContent = "";
        out.classList.remove("is-over");
        return;
      }
      out.textContent = group(rows) + (rows === 1 ? " rad" : " rader") + " under rubrikerna";
      out.classList.toggle("is-over", max > 0 && rows > max);
    }
    field.addEventListener("input", update);
    update();
  }

  function setupProof(form) {
    var proof = form.querySelector("[data-im-proof]");
    if (!proof) {
      return;
    }
    var radios = form.querySelectorAll('input[name="choice"]');
    function update() {
      var picked = form.querySelector('input[name="choice"]:checked');
      proof.hidden = !!picked && picked.value === "unknown";
    }
    Array.prototype.forEach.call(radios, function (radio) {
      radio.addEventListener("change", update);
    });
    update();
  }

  function setupNewList(select) {
    var form = select.form;
    var box = form && form.querySelector("[data-im-newlist]");
    if (!box) {
      return;
    }
    function update() {
      box.hidden = select.value !== "new";
    }
    select.addEventListener("change", update);
    update();
  }

  function setupBusy(form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit]");
      if (!button || button.disabled) {
        return;
      }
      // Efter att formuläret skickats: ett andra klick ska inte skicka igen.
      window.setTimeout(function () {
        button.disabled = true;
        button.textContent = form.getAttribute("data-im-busy");
      }, 0);
    });
  }

  function init() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-im-poll]"), setupPoll);
    Array.prototype.forEach.call(document.querySelectorAll("[data-im-count]"), setupCount);
    Array.prototype.forEach.call(document.querySelectorAll("[data-im-consent]"), setupProof);
    Array.prototype.forEach.call(document.querySelectorAll("[data-im-list]"), setupNewList);
    Array.prototype.forEach.call(document.querySelectorAll("[data-im-busy]"), setupBusy);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
