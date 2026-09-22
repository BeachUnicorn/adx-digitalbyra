/*
  Mobilmenyn - samma för panelen och kundportalen (.mob-menu i
  manage-skin.css). Hamburgaren ([data-menu-open], aria-controls pekar på
  menyn) öppnar; krysset, Esc eller en länk stänger. Fokus flyttas in i
  menyn och tillbaka till hamburgaren, och sidan bakom scrollar inte.
*/
(function () {
  "use strict";
  var opener = document.querySelector("[data-menu-open]");
  var menu = opener && document.getElementById(opener.getAttribute("aria-controls"));
  if (!opener || !menu) return;
  var closer = menu.querySelector("[data-menu-close]");

  function open() {
    menu.hidden = false;
    opener.setAttribute("aria-expanded", "true");
    document.body.classList.add("mob-menu-open");
    if (closer) closer.focus();
  }
  function close() {
    if (menu.hidden) return;
    menu.hidden = true;
    opener.setAttribute("aria-expanded", "false");
    document.body.classList.remove("mob-menu-open");
    opener.focus();
  }
  opener.addEventListener("click", open);
  if (closer) closer.addEventListener("click", close);
  menu.addEventListener("click", function (e) { if (e.target.closest("a")) close(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  // Blir skärmen bred (surfplatta vänds) finns raden igen - menyn ska inte hänga kvar.
  window.matchMedia("(min-width: 761px)").addEventListener("change", function (m) { if (m.matches) close(); });
})();
