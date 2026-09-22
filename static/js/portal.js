/* Kundportalen: exempeltexterna på "Nytt ärende". Beskrivningen är en
   Tiptap-editor (dist/tiptap-editor.js) när JavaScript finns; utan
   JavaScript är den en vanlig textarea och allt fungerar ändå. */
(function () {
  "use strict";
  var toggle = document.querySelector("[data-examples-toggle]");
  var box = document.querySelector("[data-examples]");
  if (!toggle || !box) return;

  toggle.addEventListener("click", function () {
    var open = box.hasAttribute("hidden");
    if (open) box.removeAttribute("hidden"); else box.setAttribute("hidden", "");
    toggle.textContent = open ? "Dölj exempel" : "Visa exempel";
    if (open) box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  box.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-example-use]"); if (!btn) return;
    var text = btn.closest(".pt-example").querySelector("[data-example-text]").textContent.trim();
    var desc = document.querySelector('textarea[name="description"]');
    if (desc && (!desc.value.trim() || window.confirm("Ersätta det du redan skrivit med exemplet?"))) {
      if (desc.tiptapSetText) desc.tiptapSetText(text); else desc.value = text;
    }
    var urgency = btn.getAttribute("data-urgency");
    if (urgency) { var u = document.querySelector('input[name="urgency"][value="' + urgency + '"]'); if (u) u.checked = true; }
    if (desc) { desc.focus(); desc.scrollIntoView({ behavior: "smooth", block: "center" }); }
  });
})();

/* Mobilmenyn: hamburgaren öppnar .pt-menu, krysset/Esc/en länk stänger.
   Fokus flyttas in i menyn och tillbaka till hamburgaren. */
(function () {
  "use strict";
  var menu = document.getElementById("pt-menu");
  var opener = document.querySelector("[data-pt-menu-open]");
  if (!menu || !opener) return;
  var closer = menu.querySelector("[data-pt-menu-close]");

  function open() {
    menu.hidden = false;
    opener.setAttribute("aria-expanded", "true");
    document.body.classList.add("pt-menu-open");
    if (closer) closer.focus();
  }
  function close() {
    if (menu.hidden) return;
    menu.hidden = true;
    opener.setAttribute("aria-expanded", "false");
    document.body.classList.remove("pt-menu-open");
    opener.focus();
  }
  opener.addEventListener("click", open);
  if (closer) closer.addEventListener("click", close);
  menu.addEventListener("click", function (e) { if (e.target.closest("a")) close(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  // Blir skärmen bred (surfplatta vänds) finns raden igen - menyn ska inte hänga kvar.
  window.matchMedia("(min-width: 761px)").addEventListener("change", function (m) { if (m.matches) close(); });
})();
