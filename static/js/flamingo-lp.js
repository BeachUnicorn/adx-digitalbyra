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

   Utskick (apps/utskick, README C.2, E.4): ett besök från ett sms bär
   ?ut=<token>. Skriptet läser den en gång, tar bort den ur adressfältet med
   history.replaceState (en kopierad länk ska inte bära mottagarens token)
   och skickar den med klicket på numret. Formuläret har den redan som dolt
   fält från servern. Med body[data-fl-visit] (bara besök från ett utskick
   hos samma konto, aldrig för byrån eller demot) skickas tiden sidan varit
   synlig (document.visibilityState och performance.now) med sendBeacon när
   sidan döljs eller lämnas, högst tre gånger per sidvisning. Ingen kaka,
   ingen lagring.
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

  /* Adressens parametrar som de var när sidan öppnades (innan ut tas bort). */
  var params = new URLSearchParams(window.location.search);
  var ut = params.get("ut") || "";
  if (!/^[A-Za-z0-9]{1,12}\.[A-Za-z0-9]{10}$/.test(ut)) {
    ut = "";
  }
  if (params.has("ut") && window.history && window.history.replaceState) {
    var rest = new URLSearchParams(window.location.search);
    rest.delete("ut");
    var query = rest.toString();
    try {
      window.history.replaceState(
        window.history.state,
        "",
        window.location.pathname + (query ? "?" + query : "") + window.location.hash
      );
    } catch (e) {
      /* adressen står kvar, inget annat händer */
    }
  }

  /* Tiden på sidan för ett besök från ett utskick. */
  var visit = document.body.getAttribute("data-fl-visit") || "";
  if (ut && visit && navigator.sendBeacon && window.performance) {
    var shown = 0;
    var since = document.visibilityState === "visible" ? performance.now() : null;
    var visits = 0;
    var flush = function () {
      if (since !== null) {
        shown += performance.now() - since;
        since = null;
      }
      if (visits >= 3) {
        return;
      }
      visits += 1;
      var body = new URLSearchParams();
      body.append("ut", ut);
      body.append("s", String(Math.min(Math.round(shown / 1000), 1800)));
      try {
        navigator.sendBeacon(visit, body);
      } catch (e) {
        /* tiden räknas inte, sidan fungerar ändå */
      }
    };
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        flush();
      } else if (since === null) {
        since = performance.now();
      }
    });
    window.addEventListener("pagehide", flush);
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
    "ut",
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
    TRACKING.forEach(function (name) {
      var value = name === "ut" ? ut : params.get(name);
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
