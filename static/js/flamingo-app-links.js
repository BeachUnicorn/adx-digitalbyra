/* ==========================================================================
   ADX Flamingo, Länkar och Spårningsskript (templates/flamingo/app/utskick/
   links.html, link_form.html, link.html och snippet.html; S4,
   länk-byggaren), sidbyggarens adress (templates/flamingo/app/pages/
   editor.html, Kopiera) och utskickets länkar (step_innehall.html, Förvälj
   svar). Varje sida fungerar utan skript; det här gör dem enklare.

   1. Kopiera: <button data-ln-copy="text" hidden> visas när webbläsaren kan
      skriva till urklipp och kopierar texten (adressen eller skriptets rad).
   2. Fält som bara gäller ett val: [data-ln-show-when="namn=värde"] visas
      bara när radioknappen namn i samma formulär har värdet.
   3. Slugen: input[data-ln-slug] får beskrivningens förslag som
      platshållare medan fältet är tomt (servern gör samma förslag).
   4. Förvälj svar: select[data-ln-preselect="<id på sidans väljare>"] har en
      optgroup[data-ln-campaign] per Flamingo-sida och visar bara den valda
      sidans grupp (de andra tas ur väljaren och läggs tillbaka när sidan
      byts). Ett val som inte finns i gruppen blir Inget förval. Fältet
      ([data-ln-preselect-field]) döljs när den valda sidan inget flerval har,
      och med data-ln-when="namn=värde" också när radioknappen namn inte har
      värdet. Utan skript syns alla grupper och servern prövar valet.
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

  function setupPreselect(select) {
    var form = select.closest("form");
    var source = document.getElementById(select.getAttribute("data-ln-preselect") || "");
    var field = select.closest("[data-ln-preselect-field]") || select;
    if (!form || !source) {
      return;
    }
    var groups = Array.prototype.slice.call(select.querySelectorAll("optgroup[data-ln-campaign]"));
    var rule = (field.getAttribute("data-ln-when") || "").split("=");
    function wanted() {
      if (rule.length !== 2) {
        return true;
      }
      var checked = form.querySelector('input[name="' + rule[0] + '"]:checked');
      return Boolean(checked) && checked.value === rule[1];
    }
    function update() {
      var current = select.value;
      var match = null;
      groups.forEach(function (group) {
        if (group.getAttribute("data-ln-campaign") === source.value) {
          match = group;
        }
        if (group.parentNode === select) {
          select.removeChild(group);
        }
      });
      if (match) {
        select.appendChild(match);
      }
      select.value = current;
      if (select.value !== current) {
        select.value = "";
      }
      field.hidden = !match || !wanted();
    }
    source.addEventListener("change", update);
    if (rule.length === 2) {
      form.querySelectorAll('input[name="' + rule[0] + '"]').forEach(function (input) {
        input.addEventListener("change", update);
      });
    }
    update();
  }

  document.querySelectorAll("[data-ln-copy]").forEach(setupCopy);
  document.querySelectorAll("[data-ln-show-when]").forEach(setupShowWhen);
  document.querySelectorAll("input[data-ln-slug]").forEach(setupSlug);
  document.querySelectorAll("select[data-ln-preselect]").forEach(setupPreselect);
})();
