/* SMS-API:t (apps/sms): kundportalens SMS-sidor och byråns översikt.
   - Staplarna: siffrorna syns vid hovring och fokus (CSS); ett tryck på
     en stapel visar dem på en telefon, där fokus inte alltid följer med.
   - Kopiera-knapparna för nyckeln och kodexemplen.
   - Bekräftelse innan en nyckel återkallas eller en månad stängs.
   Allt fungerar utan skriptet, utom kopieringen. */
(function () {
  "use strict";

  function closeBars(except) {
    document.querySelectorAll(".sms-bar.is-open").forEach(function (bar) {
      if (bar !== except) bar.classList.remove("is-open");
    });
  }

  document.addEventListener("click", function (e) {
    var bar = e.target.closest("[data-sms-bar]");
    closeBars(bar);
    if (bar) bar.classList.toggle("is-open");
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeBars(null);
  });

  document.addEventListener("click", function (e) {
    var button = e.target.closest("[data-copy]");
    if (!button) return;
    var source = document.querySelector(button.getAttribute("data-copy"));
    if (!source) return;
    var text = source.textContent;
    var label = button.textContent;
    function done(ok) {
      button.textContent = ok ? "Kopierat" : "Markera och kopiera";
      window.setTimeout(function () { button.textContent = label; }, 1800);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      var range = document.createRange();
      range.selectNodeContents(source);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
      done(ok);
    }
  });

  document.addEventListener("submit", function (e) {
    var form = e.target.closest("form[data-sms-confirm]");
    if (form && !window.confirm(form.getAttribute("data-sms-confirm"))) e.preventDefault();
  });
})();
