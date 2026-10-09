/* ==========================================================================
   ADX Flamingo, Kontakter (templates/flamingo/app/kontakter/).

   Små hjälpare; varje sida fungerar utan skript (då syns alla val):
   1. Massändringen, <form data-kt-bulk>: kryssrutan i rubriken
      (data-kt-select-page) markerar sidan, raden under tabellen räknar de
      markerade (data-kt-count) och klistras mot skärmens nederkant
      (.is-active). "Markera alla som matchar" (data-kt-all) visas först när
      hela sidan är markerad. Valet i data-kt-action visar bara fälten som
      hör till åtgärden (data-kt-for="list_add").
   2. En lista eller tagg som kan vara ny: <select data-kt-new="id"> visar
      namnfältet med id:t bara när "Ny lista" eller "Ny tagg" är valt.
   3. Filtren: <form data-kt-autosubmit> skickas när en lista ändras.
   4. Kopiera: <button data-kt-copy="text" hidden> visas och kopierar.
   ========================================================================== */
(function () {
  "use strict";

  function group(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "\u00a0");
  }

  function setupBulk(form) {
    var boxes = Array.prototype.slice.call(form.querySelectorAll("[data-kt-select]"));
    var pageBox = form.querySelector("[data-kt-select-page]");
    var bar = form.querySelector("[data-kt-bulkbar]");
    var count = form.querySelector("[data-kt-count]");
    var all = form.querySelector("[data-kt-all]");
    var allWrap = form.querySelector("[data-kt-all-wrap]");
    var action = form.querySelector("[data-kt-action]");
    var total = parseInt(form.getAttribute("data-kt-total") || "0", 10);
    var idle = count ? count.textContent : "";
    form.classList.add("is-js");

    function update() {
      var checked = boxes.filter(function (box) { return box.checked; }).length;
      var whole = checked > 0 && checked === boxes.length;
      if (pageBox) {
        pageBox.checked = whole;
        pageBox.indeterminate = checked > 0 && !whole;
      }
      if (allWrap) {
        allWrap.hidden = !whole;
        if (!whole && all) {
          all.checked = false;
        }
      }
      var everything = all && all.checked;
      if (bar) {
        bar.classList.toggle("is-active", checked > 0);
      }
      if (count) {
        if (everything) {
          count.textContent = "Alla " + group(total) + " som matchar är markerade.";
        } else if (checked) {
          count.textContent = group(checked) + (checked === 1 ? " markerad." : " markerade.");
        } else {
          count.textContent = idle;
        }
      }
    }

    function showFor() {
      if (!action) {
        return;
      }
      var value = action.value;
      form.querySelectorAll("[data-kt-for]").forEach(function (el) {
        var keys = el.getAttribute("data-kt-for").split(" ");
        el.hidden = keys.indexOf(value) === -1;
      });
      form.querySelectorAll("[data-kt-only]").forEach(function (option) {
        var hide = option.getAttribute("data-kt-only") !== value;
        option.hidden = hide;
        option.disabled = hide;
        if (hide && option.selected) {
          option.parentNode.value = "";
          option.parentNode.dispatchEvent(new Event("change"));
        }
      });
    }

    boxes.forEach(function (box) { box.addEventListener("change", update); });
    if (pageBox) {
      pageBox.addEventListener("change", function () {
        boxes.forEach(function (box) { box.checked = pageBox.checked; });
        update();
      });
    }
    if (all) {
      all.addEventListener("change", update);
    }
    if (action) {
      action.addEventListener("change", showFor);
      showFor();
    }
    update();
  }

  function setupNew(select) {
    var input = document.getElementById(select.getAttribute("data-kt-new"));
    if (!input) {
      return;
    }
    var label = input.id ? document.querySelector('label[for="' + input.id + '"]') : null;
    function update() {
      var isNew = select.value === "ny";
      input.hidden = !isNew;
      input.required = isNew;
      if (label && !label.classList.contains("fl-sr")) {
        label.hidden = !isNew;
      }
    }
    select.addEventListener("change", update);
    update();
  }

  function setupAutosubmit(form) {
    form.querySelectorAll("select").forEach(function (select) {
      select.addEventListener("change", function () {
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      });
    });
  }

  function setupCopy(button) {
    if (!navigator.clipboard) {
      return;
    }
    var text = button.textContent;
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(button.getAttribute("data-kt-copy")).then(function () {
        button.textContent = "Kopierad";
        window.setTimeout(function () { button.textContent = text; }, 2000);
      });
    });
  }

  document.querySelectorAll("[data-kt-bulk]").forEach(setupBulk);
  document.querySelectorAll("[data-kt-new]").forEach(setupNew);
  document.querySelectorAll("[data-kt-autosubmit]").forEach(setupAutosubmit);
  document.querySelectorAll("[data-kt-copy]").forEach(setupCopy);
})();
