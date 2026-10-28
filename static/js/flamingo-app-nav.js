/* ==========================================================================
   ADX Flamingo, verktygets flikrad (templates/flamingo/app/_tabs.html).

   Under 560 px rullar flikraden i sidled inne i raden. Skriptet rullar fram
   den aktiva fliken när sidan laddas, så att den syns också när den ligger
   långt till höger (Leveranshälsa i Sms och e-post). Valfritt: utan skript
   går det att rulla själv, och ingen flik klipps bort.
   ========================================================================== */
(function () {
  "use strict";

  function reveal(row) {
    var tab = row.querySelector('[aria-current="page"]');
    if (!tab || row.scrollWidth <= row.clientWidth) {
      return;
    }
    // Bara radens egen rullning: scrollIntoView skulle också rulla sidan.
    var left = tab.offsetLeft - (row.clientWidth - tab.offsetWidth) / 2;
    row.scrollLeft = Math.max(0, left);
  }

  function init() {
    var rows = document.querySelectorAll("[data-fl-tabs]");
    for (var i = 0; i < rows.length; i++) {
      reveal(rows[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
