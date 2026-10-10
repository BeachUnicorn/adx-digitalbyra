/* ==========================================================================
   ADX Flamingo, Länkar och Spårningsskript (templates/flamingo/app/utskick/
   links.html, link_form.html, link.html och snippet.html; S4,
   länk-byggaren). Varje sida fungerar utan skript; det här gör dem enklare.

   1. Kopiera: <button data-ln-copy="text" hidden> visas när webbläsaren kan
      skriva till urklipp och kopierar texten (adressen eller skriptets rad).
   2. Fält som bara gäller ett val: [data-ln-show-when="namn=värde"] visas
      bara när radioknappen namn i samma formulär har värdet.
   3. Slugen: input[data-ln-slug] får beskrivningens förslag som
      platshållare medan fältet är tomt (servern gör samma förslag).
   ========================================================================== */
(function () {
  "use strict";

  function setupCopy(button) {
    if (!navigator.clipboard || !window.isSecureContext) {
      return;
    }
    var label = button.innerHTML;
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(button.getAttribute("data-ln-copy") || "").then(function () {
        button.textContent = "Kopierad";
        window.setTimeout(function () { button.innerHTML = label; }, 2000);
      });
    });
  }

  function setupShowWhen(block) {
    var rule = (block.getAttribute("data-ln-show-when") || "").split("=");
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

  function slugFrom(text) {
    var value = String(text || "").toLowerCase();
    if (value.normalize) {
      value = value.normalize("NFKD").replace(/[̀-ͯ]/g, "");
    }
    return value.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "");
  }

  function setupSlug(input) {
    var form = input.closest("form");
    var label = form && form.querySelector("[data-ln-label]");
    if (!label) {
      return;
    }
    var fallback = input.getAttribute("placeholder") || "";
    label.addEventListener("input", function () {
      input.setAttribute("placeholder", slugFrom(label.value) || fallback);
    });
  }

  document.querySelectorAll("[data-ln-copy]").forEach(setupCopy);
  document.querySelectorAll("[data-ln-show-when]").forEach(setupShowWhen);
  document.querySelectorAll("input[data-ln-slug]").forEach(setupSlug);
})();
