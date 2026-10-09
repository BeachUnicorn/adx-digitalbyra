/* ==========================================================================
   ADX Flamingo, Utskick (templates/flamingo/app/utskick/ och svarstråden i
   Inkorgen). Varje sida fungerar utan skript; det här gör den snabbare.

   1. Sms-räknaren (README I.2, G.2; kontraktet i S2-HANDOFF.md): varje
      <textarea data-ut-sms data-ut-sms-count="id"> skriver "GSM-7 · 134 av
      160 · 1 del" i elementet med id:t, räknat som apps/sms/encoding.analyse
      (GSM-7 160/153, UCS-2 70/67, utökade tecken kostar två platser, ett
      teckenpar delas aldrig mellan delar).
        data-ut-sms-ore-per-part="39"   lägger till " · 0,39 kr"
        data-ut-sms-recipients="388"    gör det till " · cirka 151 kr" (hela utskicket)
        data-ut-sms-suffix=" /Exempelrör"   räknas men skrivs inte (svar från Inkorgen)
      Utskickets redigerare har också:
        data-ut-sms-optout="Svara STOPP ..." och data-ut-sms-optout-mode="reply|name"
                                        raden som läggs till sist (räknas)
        data-ut-sms-link="k.adx.se/a8Kf2X"  en länk som den skrivs (lika lång som en riktig)
        data-ut-sms-preview="url"       den exakta förhandsvisningen från servern
                                        (app_utskick_sms_preview, POST sms_body),
                                        en stund efter att kunden slutat skriva
        data-ut-sms-bubble="id"         telefonens pratbubbla som skrivs om
        data-ut-sms-notes="id"          felen och varningarna under texten
        data-ut-sms-gsm="id"            rutan om dyra tecken (Byt automatiskt):
                                        visas när servern hittar sådana tecken,
                                        [data-ut-gsm-chars] får tecknen i klartext
      Platshållarna räknas ungefär tills servern svarat: {länk:x} som en kort
      länk (15 tecken), {förnamn|du} som reservtexten, andra värden som Anna.
   2. Mottagarnas antal, <form data-ut-count="url">: när en ruta kryssas
      hämtas antalet för det osparade urvalet (app_utskick_count?urval=1) och
      skrivs i [data-ut-count-total], [data-ut-count-text] och
      [data-ut-count-weekly].
   3. Infoga en platshållare: <button data-ut-insert="{förnamn}"
      data-ut-target="id" hidden> visas och skriver där markören står.
      [data-ut-insert-row] visas och [data-ut-insert-plain] döljs.
   4. Fält som bara gäller ett val: [data-ut-show-when="namn=värde"] visas
      bara när radioknappen namn har värdet.
   5. Bekräftelsen: <button data-ut-dialog="id"> öppnar <dialog id> i stället
      för att skicka; [data-ut-dialog-ok] skickar formuläret,
      [data-ut-dialog-cancel] stänger (README I.4: Skicka nu upprepar siffrorna).
   6. Länkkontrollen: <form data-ut-linkcheck> frågar servern utan att lämna
      sidan och skriver svaret i [data-ut-linkcheck-result].
   7. Enter i fälten för en ny länk eller testnumret trycker deras egen knapp.
   8. Guidens steg, <form data-ut-guard>: något ändrat men inte sparat gör att
      webbläsaren frågar innan sidan lämnas (menyn, bakåt). Varje knapp som
      sparar steget (också stegchipsen och "byt kontakt") släpper frågan.
   ========================================================================== */
(function () {
  "use strict";

  var NBSP = String.fromCharCode(160);
  var GSM_BASIC =
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?" +
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";
  var GSM_EXTENDED = "^{}\\[~]|€\f";
  var SAMPLE_LINK = "k.adx.se/a8Kf2X";
  var STOP_RE = /svara\s+stopp/i;
  var TOKEN_RE = /\{([^{}\n]{1,80})\}/g;

  function group(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  }

  // ---------------------------------------------------------------- räknaren

  function countParts(widths, single, perPart) {
    var total = widths.reduce(function (sum, w) { return sum + w; }, 0);
    if (total === 0) {
      return 0;
    }
    if (total <= single) {
      return 1;
    }
    var parts = 1;
    var used = 0;
    widths.forEach(function (w) {
      if (used + w > perPart) {
        parts += 1;
        used = 0;
      }
      used += w;
    });
    return parts;
  }

  function analyse(text) {
    var chars = Array.from(text || "");
    var gsm = chars.every(function (ch) {
      return GSM_BASIC.indexOf(ch) !== -1 || GSM_EXTENDED.indexOf(ch) !== -1;
    });
    var widths = chars.map(function (ch) {
      if (gsm) {
        return GSM_EXTENDED.indexOf(ch) !== -1 ? 2 : 1;
      }
      return ch.codePointAt(0) > 0xffff ? 2 : 1;
    });
    var units = widths.reduce(function (sum, w) { return sum + w; }, 0);
    var single = gsm ? 160 : 70;
    var perPart = gsm ? 153 : 67;
    var parts = countParts(widths, single, perPart);
    var room = parts <= 1 ? single : perPart * parts;
    return { gsm: gsm, units: units, parts: parts, room: room };
  }

  function counterText(result) {
    var label = result.gsm ? "GSM-7" : "UCS-2";
    var parts = result.parts === 1 ? "1 del" : result.parts + " delar";
    return label + " · " + result.units + " av " + result.room + " · " + parts;
  }

  function oreText(ore) {
    var kr = Math.floor(ore / 100);
    var rest = String(ore % 100);
    return kr + "," + (rest.length < 2 ? "0" + rest : rest) + NBSP + "kr";
  }

  function costText(field, parts) {
    var perPart = parseInt(field.getAttribute("data-ut-sms-ore-per-part") || "0", 10);
    if (!perPart || !parts) {
      return "";
    }
    var recipients = parseInt(field.getAttribute("data-ut-sms-recipients") || "0", 10);
    if (recipients > 0) {
      var kr = Math.ceil((recipients * parts * perPart) / 100);
      return " · cirka " + group(kr) + NBSP + "kr";
    }
    return " · " + oreText(perPart * parts);
  }

  function approximate(field, value) {
    // Utskickets text som mottagaren ungefär får den (servern räknar exakt).
    var optout = field.getAttribute("data-ut-sms-optout");
    var link = field.getAttribute("data-ut-sms-link") || SAMPLE_LINK;
    var text = value.replace(TOKEN_RE, function (whole, inner) {
      var name = inner.split("|")[0].trim().toLowerCase();
      if (name.indexOf("länk:") === 0) {
        return link;
      }
      if (name === "avregistrering") {
        return whole;
      }
      var bar = inner.indexOf("|");
      if (bar !== -1 && inner.slice(bar + 1).trim()) {
        return inner.slice(bar + 1).trim();
      }
      return "Anna";
    });
    if (!optout) {
      return text;
    }
    var mode = field.getAttribute("data-ut-sms-optout-mode") || "reply";
    if (mode !== "reply") {
      text = text.replace(/\s*Svara STOPP för att inte få fler sms\./g, "");
    }
    if (text.indexOf("{avregistrering}") !== -1) {
      return text.replace(/\{avregistrering\}/g, optout).trim();
    }
    if (mode === "reply" && STOP_RE.test(text)) {
      return text.trim();
    }
    return text.replace(/\s+$/, "") + "\n" + optout;
  }

  function csrfToken(el) {
    var form = el.form || el.closest("form");
    var input = form ? form.querySelector("input[name=csrfmiddlewaretoken]") : null;
    return input ? input.value : "";
  }

  function renderNotes(target, notes) {
    if (!target) {
      return;
    }
    while (target.firstChild) {
      target.removeChild(target.firstChild);
    }
    (notes || []).forEach(function (note) {
      var p = document.createElement("p");
      p.className = note.level === "error" ? "fl-field__error" : "fl-ut-warn";
      p.textContent = note.text;
      target.appendChild(p);
    });
  }

  function setupCounter(field) {
    var out = document.getElementById(field.getAttribute("data-ut-sms-count"));
    if (!out) {
      return;
    }
    var suffix = field.getAttribute("data-ut-sms-suffix") || "";
    var previewUrl = field.getAttribute("data-ut-sms-preview");
    var bubble = document.getElementById(field.getAttribute("data-ut-sms-bubble") || "");
    var notes = document.getElementById(field.getAttribute("data-ut-sms-notes") || "");
    var gsm = document.getElementById(field.getAttribute("data-ut-sms-gsm") || "");
    var timer = null;
    var asked = 0;

    function update() {
      // Radbrytningar skickas som \n, oavsett vad webbläsaren har i rutan.
      var value = field.value.replace(/\r\n?/g, "\n");
      var result = analyse(approximate(field, value) + suffix);
      out.textContent = counterText(result) + costText(field, result.parts);
      out.classList.toggle("is-over", result.parts > 6);
      if (previewUrl && window.fetch) {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () { ask(value); }, 450);
      }
    }

    function ask(value) {
      asked += 1;
      var mine = asked;
      var body = new URLSearchParams();
      body.append("sms_body", value);
      fetch(previewUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-CSRFToken": csrfToken(field),
          "X-Requested-With": "XMLHttpRequest",
          Accept: "application/json"
        },
        body: body
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data || mine !== asked) {
            return;
          }
          out.textContent = data.counter + (data.total_text ? " · cirka " + data.total_text : "");
          out.classList.toggle("is-over", Boolean(data.too_long));
          if (bubble) {
            bubble.textContent = data.text;
            bubble.classList.add("is-live");
          }
          renderNotes(notes, data.notes);
          if (gsm) {
            var odd = data.non_gsm || [];
            var chars = gsm.querySelector("[data-ut-gsm-chars]");
            if (chars) {
              chars.textContent = data.non_gsm_text || odd.join(" ");
            }
            gsm.hidden = odd.length === 0;
          }
        })
        .catch(function () { /* Räknaren ovan står kvar. */ });
    }

    field.addEventListener("input", update);
    update();
  }

  // ---------------------------------------------------------------- antalet

  function setupCount(form) {
    var url = form.getAttribute("data-ut-count");
    var total = form.querySelector("[data-ut-count-total]");
    var text = form.querySelector("[data-ut-count-text]");
    var weekly = form.querySelector("[data-ut-count-weekly]");
    var names = ["lists", "tags", "contacts", "exclude_lists", "exclude_tags", "exclude_recent"];
    var timer = null;
    var asked = 0;

    function query() {
      var params = new URLSearchParams();
      params.append("urval", "1");
      names.forEach(function (name) {
        form.querySelectorAll('input[name="' + name + '"]:checked').forEach(function (box) {
          params.append(name, box.value);
        });
      });
      return params.toString();
    }

    function ask() {
      asked += 1;
      var mine = asked;
      fetch(url + "?" + query(), {
        credentials: "same-origin",
        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" }
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data || mine !== asked) {
            return;
          }
          if (total) { total.textContent = data.total_text; }
          if (text) { text.textContent = data.text; }
          if (weekly) { weekly.textContent = data.weekly_text || ""; }
        })
        .catch(function () { /* Siffrorna från sidan står kvar. */ });
    }

    form.addEventListener("change", function (event) {
      if (names.indexOf(event.target.name) === -1 || !window.fetch) {
        return;
      }
      window.clearTimeout(timer);
      timer = window.setTimeout(ask, 300);
    });
  }

  // ---------------------------------------------------------------- infoga

  function insertAtCursor(field, token) {
    var start = field.selectionStart;
    var end = field.selectionEnd;
    if (typeof start !== "number") {
      start = end = field.value.length;
    }
    var before = field.value.slice(0, start);
    var after = field.value.slice(end);
    var pad = before && !/\s$/.test(before) ? " " : "";
    field.value = before + pad + token + after;
    var at = before.length + pad.length + token.length;
    field.focus();
    field.setSelectionRange(at, at);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function setupInsert() {
    document.querySelectorAll("[data-ut-insert-row]").forEach(function (row) { row.hidden = false; });
    document.querySelectorAll("[data-ut-insert-plain]").forEach(function (p) { p.hidden = true; });
    document.querySelectorAll("button[data-ut-insert]").forEach(function (button) {
      var field = document.getElementById(button.getAttribute("data-ut-target"));
      if (!field) {
        return;
      }
      button.hidden = false;
      button.addEventListener("click", function () {
        insertAtCursor(field, button.getAttribute("data-ut-insert"));
      });
    });
  }

  // ---------------------------------------------------------------- visa när

  function setupShowWhen(block) {
    var rule = (block.getAttribute("data-ut-show-when") || "").split("=");
    var form = block.closest("form");
    if (rule.length !== 2 || !form) {
      return;
    }
    function update() {
      var checked = form.querySelector('input[name="' + rule[0] + '"]:checked');
      block.hidden = !checked || checked.value !== rule[1];
    }
    form.querySelectorAll('input[name="' + rule[0] + '"]').forEach(function (input) {
      input.addEventListener("change", update);
    });
    update();
  }

  // ---------------------------------------------------------------- dialogen

  function setupDialog(button) {
    var dialog = document.getElementById(button.getAttribute("data-ut-dialog"));
    var form = button.form;
    if (!dialog || !form || typeof dialog.showModal !== "function") {
      return;
    }
    button.addEventListener("click", function (event) {
      if (!form.checkValidity()) {
        return;
      }
      event.preventDefault();
      dialog.showModal();
    });
    var ok = dialog.querySelector("[data-ut-dialog-ok]");
    var cancel = dialog.querySelector("[data-ut-dialog-cancel]");
    if (ok) {
      ok.addEventListener("click", function () {
        ok.disabled = true;
        dialog.close();
        form.submit();
      });
    }
    if (cancel) {
      cancel.addEventListener("click", function () { dialog.close(); });
    }
  }

  // ---------------------------------------------------------------- länkkontrollen

  function setupLinkCheck(form) {
    var result = form.querySelector("[data-ut-linkcheck-result]");
    var button = form.querySelector("button[type=submit]");
    form.addEventListener("submit", function (event) {
      if (!window.fetch) {
        return;
      }
      event.preventDefault();
      if (button) { button.disabled = true; }
      if (result) { result.textContent = "Kontrollerar länkarna."; }
      fetch(form.action, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-CSRFToken": csrfToken(form),
          "X-Requested-With": "XMLHttpRequest",
          Accept: "application/json"
        }
      })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (result) {
            result.textContent = data ? data.text : "Det gick inte att kontrollera länkarna nu.";
          }
        })
        .catch(function () {
          if (result) { result.textContent = "Det gick inte att kontrollera länkarna nu."; }
        })
        .then(function () { if (button) { button.disabled = false; } });
    });
  }

  // ---------------------------------------------------------------- Enter

  function setupEnter(container, buttonSelector) {
    var button = container.querySelector(buttonSelector);
    if (!button) {
      return;
    }
    container.querySelectorAll("input").forEach(function (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          button.click();
        }
      });
    });
  }

  // ---------------------------------------------------------------- osparat

  function setupGuard(form) {
    var dirty = false;
    function mark() { dirty = true; }
    form.addEventListener("input", mark);
    form.addEventListener("change", mark);
    form.addEventListener("submit", function () { dirty = false; });
    window.addEventListener("beforeunload", function (event) {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
  }

  document.querySelectorAll("textarea[data-ut-sms]").forEach(setupCounter);
  document.querySelectorAll("form[data-ut-guard]").forEach(setupGuard);
  document.querySelectorAll("form[data-ut-count]").forEach(setupCount);
  document.querySelectorAll("[data-ut-show-when]").forEach(setupShowWhen);
  document.querySelectorAll("button[data-ut-dialog]").forEach(setupDialog);
  document.querySelectorAll("form[data-ut-linkcheck]").forEach(setupLinkCheck);
  document.querySelectorAll(".fl-ut-linkform").forEach(function (box) {
    setupEnter(box, 'button[value="lank"]');
  });
  document.querySelectorAll(".fl-ut-testrow").forEach(function (row) {
    setupEnter(row, 'button[name="till"]');
  });
  setupInsert();
})();
