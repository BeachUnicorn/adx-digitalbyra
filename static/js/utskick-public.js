/*
 * De publika sidorna för utskick (templates/utskick/public/, README I.1):
 * bara botskyddets JS-bevis, samma som i static/js/site.js. Fältet
 * bc_proof fylls vid första verkliga interaktionen med formuläret, med
 * värdet ur formulärets data-botcheck. En bot som bara postar HTML:en
 * lämnar det tomt (apps/common/botcheck.py). Inga kakor, ingen lagring,
 * ingen statistik.
 */
(function () {
  'use strict';
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
