/**
 * Live price calculator for the service form (/manage/services/).
 *
 * Inputs are NET (exkl moms): labour + material. This previews:
 *   - total net (exkl moms)
 *   - total gross (incl moms) - what consumers see
 *   - possible ROT deduction (rot_percentage% of labour incl moms)
 *   - price after ROT (gross − deduction)
 *
 * Read-only preview so the editor never has to do the math.
 */
(function () {
    "use strict";

    const root = document.querySelector("[data-price-calc]");
    if (!root) return;

    const rotPct = parseFloat(root.dataset.rotPercentage || "30");
    const vatPct = parseFloat(root.dataset.vatRate || "25");

    const laborFrom = document.querySelector('[name="labor_price_from"]');
    const laborTo = document.querySelector('[name="labor_price_to"]');
    const matFrom = document.querySelector('[name="material_price_from"]');
    const matTo = document.querySelector('[name="material_price_to"]');
    const rotCheckbox = document.querySelector('[name="is_rot_eligible"]');

    const netEl = root.querySelector("[data-calc-net]");
    const grossEl = root.querySelector("[data-calc-gross]");
    const rotRow = root.querySelector("[data-calc-rot-row]");
    const rotEl = root.querySelector("[data-calc-rot]");
    const afterRotRow = root.querySelector("[data-calc-after-rot-row]");
    const afterRotEl = root.querySelector("[data-calc-after-rot]");

    function num(input) {
        const v = parseInt(input && input.value, 10);
        return isNaN(v) || v < 0 ? 0 : v;
    }

    function fmt(n) {
        return Math.round(n).toLocaleString("sv-SE") + " kr";
    }

    function rangeStr(from, to) {
        if (from && to && to !== from) return fmt(from) + " – " + fmt(to);
        if (from) return "Från " + fmt(from);
        if (to) return "Från " + fmt(to);
        return "-";
    }

    function vat(n) {
        return n * (1 + vatPct / 100);
    }

    function recalc() {
        const lf = num(laborFrom), lt = num(laborTo);
        const mf = num(matFrom), mt = num(matTo);

        const netFrom = lf + mf;
        const netTo = lt + mt;
        netEl.textContent = rangeStr(netFrom, netTo);

        const grossFrom = netFrom ? vat(netFrom) : 0;
        const grossTo = netTo ? vat(netTo) : 0;
        grossEl.textContent = rangeStr(grossFrom, grossTo);

        const eligible = rotCheckbox && rotCheckbox.checked;
        if (eligible && (lf || lt)) {
            const pct = rotPct / 100;
            // ROT is calculated on labour INCL VAT.
            const dFrom = lf ? vat(lf) * pct : 0;
            const dTo = lt ? vat(lt) * pct : 0;
            rotEl.textContent = "-" + rangeStr(dFrom, dTo);
            rotRow.hidden = false;

            const afterFrom = grossFrom ? grossFrom - dFrom : 0;
            const afterTo = grossTo ? grossTo - dTo : 0;
            afterRotEl.textContent = rangeStr(afterFrom, afterTo);
            afterRotRow.hidden = false;
        } else {
            rotRow.hidden = true;
            afterRotRow.hidden = true;
        }
    }

    [laborFrom, laborTo, matFrom, matTo].forEach(function (el) {
        if (el) el.addEventListener("input", recalc);
    });
    if (rotCheckbox) rotCheckbox.addEventListener("change", recalc);

    recalc();
})();
