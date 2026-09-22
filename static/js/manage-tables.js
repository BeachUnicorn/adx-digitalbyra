/*
  Tabeller på små skärmar.

  Panelens listor har fem till åtta kolumner. I en telefonbredd rymdes bara
  de två första, och resten fanns bara om man råkade upptäcka att lådan gick
  att dra i sidled - alltså var kolumnerna i praktiken borta. I stället
  blir varje rad ett kort där cellen bär sin egen kolumnrubrik.

  Rubriken stämplas här i stället för i sjutton mallar: CSS kan inte läsa
  <thead>, och en data-label per cell i varje mall hade glömts bort i nästa
  tabell någon lägger till. Skriptet är idempotent och körs om när tavlans
  panel eller en lista ritas om (window.AdxTables.init()).

  Tabeller som redan ÄR två kolumner (etikett + värde) hoppas över med
  klassen m-table--plain; de blir inte bättre av att bli kort.
*/
(function () {
  "use strict";

  function label(table, index) {
    var head = table.tHead && table.tHead.rows[table.tHead.rows.length - 1];
    if (!head) return "";
    var cell = head.cells[index];
    return cell ? (cell.textContent || "").trim().replace(/\s+/g, " ") : "";
  }

  function stamp(table) {
    if (table.classList.contains("m-table--plain")) return;
    var bodies = table.tBodies.length ? table.tBodies : [table];
    Array.prototype.forEach.call(bodies, function (body) {
      Array.prototype.forEach.call(body.rows, function (row) {
        // Rader som spänner över hela tabellen (tomt-läget, delsummor) är
        // inga kort - de ska stå kvar som en enda rad.
        if (row.cells.length === 1 && row.cells[0].colSpan > 1) {
          row.setAttribute("data-full", "1");
          return;
        }
        Array.prototype.forEach.call(row.cells, function (cell, i) {
          if (cell.hasAttribute("data-label")) return;
          var text = label(table, i);
          // Tom rubrik = åtgärdskolumn. Den behöver ingen etikett.
          cell.setAttribute("data-label", text);
          if (!text) cell.setAttribute("data-unlabelled", "1");
          if (!(cell.textContent || "").trim() && !cell.children.length) {
            cell.setAttribute("data-blank", "1");
          }
        });
      });
    });
    table.setAttribute("data-cards", "1");
  }

  function init(root) {
    (root || document).querySelectorAll("table.m-table").forEach(stamp);
  }

  window.AdxTables = { init: init };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); });
  } else {
    init();
  }
})();

/*
  Undermenyn är EN rad som går att dra i sidled på telefon (tavla.css). Då
  måste den sida man står på synas - annars ser raden ut att sakna den.
*/
(function () {
  "use strict";
  function showActive() {
    document.querySelectorAll(".tv-subnav__links").forEach(function (row) {
      if (row.scrollWidth <= row.clientWidth + 1) return;
      var active = row.querySelector(".is-active");
      if (!active) return;
      var left = active.offsetLeft - (row.clientWidth - active.offsetWidth) / 2;
      row.scrollLeft = Math.max(0, left);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", showActive);
  } else {
    showActive();
  }
  window.addEventListener("resize", showActive);
})();
