/*
  ADX Flamingo i panelen (templates/manage/flamingo/).

  - Formulär med data-confirm frågar först (Pausa, Markera som live,
    Markera som exporterade). Panelens tavla.js gör samma sak, men laddas
    inte på de här sidorna.
  - Granskningen: skäl-fältet för en del visas när delen ändrats (utan
    skript syns alla, och servern kräver ett skäl ändå), en teckenräknare
    vid varje rubrik och beskrivning, och förhandsvisningen av annonsen
    följer de två första rubrikerna och den första beskrivningen.
*/
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.getAttribute("data-confirm"))) {
      event.preventDefault();
    }
  });

  function fieldsOf(group) {
    return Array.prototype.filter.call(
      group.querySelectorAll("input, textarea, select"),
      function (el) { return !el.closest("[data-reason]") && el.type !== "hidden"; }
    );
  }

  function valueOf(el) {
    return (el.value || "").replace(/\r\n/g, "\n").trim();
  }

  function setupGroup(group) {
    var fields = fieldsOf(group);
    var initial = fields.map(valueOf);
    var serverChanged = group.classList.contains("is-changed");
    var reason = group.querySelector("[data-reason] input");

    function update() {
      var dirty = fields.some(function (el, i) { return valueOf(el) !== initial[i]; });
      var hasReason = reason && reason.value.trim() !== "";
      group.classList.toggle("is-changed", serverChanged || dirty || hasReason);
    }
    group.addEventListener("input", update);
    group.addEventListener("change", update);
    update();
  }

  function setupCounter(el) {
    var max = parseInt(el.getAttribute("data-count"), 10);
    var label = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
    if (!max || !label) return;
    var count = document.createElement("span");
    count.className = "mf-count";
    count.setAttribute("aria-hidden", "true");
    label.appendChild(count);
    function update() {
      var n = (el.value || "").trim().length;
      count.textContent = n + "/" + max;
      count.classList.toggle("is-over", n > max);
    }
    el.addEventListener("input", update);
    update();
  }

  function setupPreview(form) {
    var serp = form.querySelector("[data-serp]");
    if (!serp) return;
    var title = serp.querySelector("[data-serp-title]");
    var desc = serp.querySelector("[data-serp-desc]");
    var h1 = form.querySelector('[name="headline_0"]');
    var h2 = form.querySelector('[name="headline_1"]');
    var d1 = form.querySelector('[name="description_0"]');
    function update() {
      var parts = [h1, h2].map(function (el) { return el ? el.value.trim() : ""; })
        .filter(function (text) { return text; });
      if (title) title.textContent = parts.join(" | ");
      if (desc && d1) desc.textContent = d1.value.trim();
    }
    [h1, h2, d1].forEach(function (el) { if (el) el.addEventListener("input", update); });
  }

  function init() {
    document.querySelectorAll("form[data-review-form]").forEach(function (form) {
      form.querySelectorAll("[data-review-group]").forEach(setupGroup);
      form.querySelectorAll("[data-count]").forEach(setupCounter);
      setupPreview(form);
      form.classList.add("is-enhanced");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
