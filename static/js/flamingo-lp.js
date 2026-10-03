/* ==========================================================================
   Kundens landningssidor (/lp/<slug>/), templates/flamingo/lp/ren/.

   Klick på numret, valfritt (sidan och numret fungerar utan skript). Varje
   a[data-fl-call] skickar ett sendBeacon till body[data-fl-beacon]
   (/lp/<slug>/ring/) med CSRF-nyckeln, klick-id:n, utm och keyword ur
   adressen. Klicket blir en förfrågan, och med gclid en konvertering hos
   Google. Samtalet hålls aldrig upp: inget preventDefault, och sendBeacon
   väntar inte på något svar. Utan data-fl-beacon (förhandsvisning, byrån,
   demokonto) skickas inget.

   Sidan frågar inte om samtycke och skickar inget svar om det (beslut
   2026-10-03). Inga kakor, ingen lagring i webbläsaren.
   ========================================================================== */
(function () {
  "use strict";

  /* Före och efter (sidbyggarens block, variant "Reglage"): reglaget flyttar
     gränsen mellan bilderna. Utan skript står gränsen i mitten. */
  Array.prototype.forEach.call(document.querySelectorAll("[data-rn-compare]"), function (figure) {
    var range = figure.querySelector("input[type=range]");
    var frame = figure.querySelector(".rn-compare__frame");
    if (!range || !frame) {
      return;
    }
    var update = function () {
      frame.style.setProperty("--pos", range.value + "%");
    };
    range.addEventListener("input", update);
    update();
  });

  /* Ringremsan i mobilen: med skript glider den upp först när knapparna i
     Toppen inte syns längre, så att numret inte står tre gånger på den
     första skärmen. Utan skript (eller IntersectionObserver) syns den hela
     tiden. Klicken räknas som förut (a[data-fl-call] nedan). */
  var callbar = document.querySelector(".rn-callbar");
  var heroActions = document.querySelector(".rn-hero__actions");
  if (callbar && heroActions && "IntersectionObserver" in window) {
    document.body.classList.add("rn--reveal");
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        callbar.classList.toggle("is-shown", !entry.isIntersecting);
      });
    }).observe(heroActions);
  }

  var beacon = document.body.getAttribute("data-fl-beacon") || "";
  var TRACKING = [
    "gclid",
    "gbraid",
    "wbraid",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "keyword",
  ];

  if (!beacon || !navigator.sendBeacon || !window.FormData) {
    return;
  }

  var sent = false;

  function send() {
    if (sent) {
      return;
    }
    sent = true;
    var data = new FormData();
    var token = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (token) {
      data.append("csrfmiddlewaretoken", token.value);
    }
    var params = new URLSearchParams(window.location.search);
    TRACKING.forEach(function (name) {
      var value = params.get(name);
      if (value) {
        data.append(name, value.slice(0, 200));
      }
    });
    try {
      navigator.sendBeacon(beacon, data);
    } catch (e) {
      /* räknas inte, men samtalet går ändå */
    }
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[data-fl-call]") : null;
    if (link) {
      send();
    }
  });
})();
