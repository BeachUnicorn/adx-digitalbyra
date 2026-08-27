/* ADX site.js - mobilmenyn ur strict-design-guide.html, plus formulärets
   JS-bevis (botskydd: fältet fylls först vid mänsklig interaktion, se
   apps/common/botcheck.py - mönsterkatalogen §6). Allt beteende bor i
   statiska filer så CSP:n kan vara strikt utan 'unsafe-inline'. */
(function () {
  'use strict';

  var menu = document.getElementById('mobileMenu');
  var open = document.getElementById('menuOpen');
  var close = document.getElementById('menuClose');
  if (menu && open && close) {
    open.addEventListener('click', function () { menu.classList.add('open'); });
    close.addEventListener('click', function () { menu.classList.remove('open'); });
    menu.addEventListener('click', function (e) {
      if (e.target.closest('a')) menu.classList.remove('open');
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') menu.classList.remove('open');
    });
  }

  // Botskyddets JS-bevis: fylls vid första verkliga interaktionen med
  // formuläret. En bot som bara POST:ar HTML:en lämnar fältet tomt.
  document.querySelectorAll('form[data-botcheck]').forEach(function (form) {
    var proof = form.querySelector('input[name="bc_proof"]');
    if (!proof) return;
    var arm = function () {
      proof.value = form.dataset.botcheck;
      form.removeEventListener('pointerdown', arm);
      form.removeEventListener('keydown', arm);
    };
    form.addEventListener('pointerdown', arm);
    form.addEventListener('keydown', arm);
  });
})();
