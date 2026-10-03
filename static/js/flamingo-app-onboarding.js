/* ==========================================================================
   ADX Flamingo, kom igång (templates/flamingo/app/onboarding/).

   Tre små saker, alla valfria (sidorna fungerar utan skript):
   1. Teckenräknaren för autosvaret: <textarea data-fl-sms-count="id"> visar
      "N tecken, M sms" i elementet med id:t. Samma räkning som
      sms_parts() i apps/flamingo/app_views/onboarding.py.
   2. Formulär med data-fl-busy: knappen visar texten och låses när
      formuläret skickas (läsningen av hemsidan tar en stund).
   3. En adress med #ny-tjanst (eller annan <details>) öppnar den rutan.
   ========================================================================== */
(function () {
  "use strict";

  // GSM 03.38: grunduppsättningen räknas som ett tecken, tillägget som två.
  var GSM_BASIC =
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡" +
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";
  var GSM_EXTENDED = "^{}\\[~]|€\f";

  function smsParts(text) {
    var chars = Array.from(text || "");
    var gsm = chars.every(function (ch) {
      return GSM_BASIC.indexOf(ch) !== -1 || GSM_EXTENDED.indexOf(ch) !== -1;
    });
    var length = 0;
    chars.forEach(function (ch) {
      if (gsm) {
        length += GSM_EXTENDED.indexOf(ch) !== -1 ? 2 : 1;
      } else {
        length += ch.codePointAt(0) > 0xffff ? 2 : 1;
      }
    });
    var single = gsm ? 160 : 70;
    var multi = gsm ? 153 : 67;
    if (length === 0) {
      return { length: 0, parts: 0 };
    }
    return { length: length, parts: length <= single ? 1 : Math.ceil(length / multi) };
  }

  function setupCounter(field) {
    var out = document.getElementById(field.getAttribute("data-fl-sms-count"));
    if (!out) {
      return;
    }
    var max = parseInt(out.getAttribute("data-max") || "0", 10);
    function update() {
      // Radbrytningar skickas som \n, oavsett vad webbläsaren har i rutan.
      var result = smsParts(field.value.replace(/\r\n?/g, "\n"));
      out.textContent = result.length + " tecken, " + result.parts + " sms";
      out.classList.toggle("is-over", max > 0 && result.length > max);
    }
    field.addEventListener("input", update);
    update();
  }

  function setupBusy(form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector("button[type=submit]");
      if (!button || button.disabled) {
        return;
      }
      // Efter att formuläret skickats, så att knappen inte saknas i det.
      window.setTimeout(function () {
        button.disabled = true;
        button.textContent = form.getAttribute("data-fl-busy");
      }, 0);
    });
  }

  function openFromHash() {
    var id = window.location.hash.slice(1);
    if (!id) {
      return;
    }
    var target = document.getElementById(id);
    if (target && target.tagName === "DETAILS") {
      target.open = true;
      var focus = target.querySelector("input:not([type=hidden])");
      if (focus) {
        focus.focus({ preventScroll: true });
      }
    }
  }

  document.querySelectorAll("textarea[data-fl-sms-count]").forEach(setupCounter);
  document.querySelectorAll("form[data-fl-busy]").forEach(setupBusy);
  openFromHash();
  window.addEventListener("hashchange", openFromHash);
})();
