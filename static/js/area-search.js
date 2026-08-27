/**
 * Live filter for the /vvs/ area directory.
 *
 * Every area is already in the document - this only hides and reveals. That
 * matters twice over: the page works without JavaScript, and every one of the
 * 200+ area links is present for crawlers rather than appearing after a script
 * runs.
 *
 * Matching rules:
 *   - the municipality name matches -> show the card as normal (capped list)
 *   - only some districts match     -> show the card with just those districts
 *   - nothing matches               -> hide the card
 */
(function () {
    "use strict";

    var input = document.querySelector("[data-area-search]");
    if (!input) return;

    var countEl = document.querySelector("[data-area-count]");
    var emptyEl = document.querySelector("[data-area-empty]");
    var termEl = document.querySelector("[data-area-term]");
    var cards = Array.prototype.slice.call(document.querySelectorAll("[data-area-card]"));
    var groups = Array.prototype.slice.call(document.querySelectorAll("[data-area-group]"));

    function escapeHtml(value) {
        return value.replace(/[&<>"]/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
        });
    }

    /** Re-render a link's label with the matching run wrapped in <mark>. */
    function label(el, needle) {
        var text = el.dataset.label || "";
        if (!needle) {
            el.textContent = text;
            return;
        }
        var index = text.toLowerCase().indexOf(needle);
        if (index < 0) {
            el.textContent = text;
            return;
        }
        el.innerHTML =
            escapeHtml(text.slice(0, index)) +
            "<mark>" +
            escapeHtml(text.slice(index, index + needle.length)) +
            "</mark>" +
            escapeHtml(text.slice(index + needle.length));
    }

    function apply(query) {
        var needle = query.trim().toLowerCase();
        var shown = 0;

        cards.forEach(function (card) {
            var kommun = card.querySelector(".c-areadir__kommun");
            var districts = Array.prototype.slice.call(card.querySelectorAll("[data-district]"));
            var more = card.querySelector("[data-more]");
            var kommunMatch = !needle || card.dataset.name.indexOf(needle) !== -1;
            var districtMatches = needle
                ? districts.filter(function (d) {
                      return d.dataset.name.indexOf(needle) !== -1;
                  })
                : [];

            if (!needle) {
                // Back to the default view: first N districts, rest collapsed.
                card.hidden = false;
                label(kommun, "");
                districts.forEach(function (d, i) {
                    d.hidden = false;
                    d.classList.toggle("is-extra", i >= 5);
                    label(d, "");
                });
                if (more) more.hidden = false;
                shown += 1;
                return;
            }

            if (!kommunMatch && !districtMatches.length) {
                card.hidden = true;
                return;
            }

            card.hidden = false;
            shown += 1;
            label(kommun, kommunMatch ? needle : "");

            if (kommunMatch) {
                // Whole municipality matched - keep the normal capped list.
                districts.forEach(function (d, i) {
                    d.hidden = false;
                    d.classList.toggle("is-extra", i >= 5);
                    label(d, needle);
                });
                if (more) more.hidden = false;
            } else {
                // Only some districts matched: show exactly those, uncapped,
                // so a hit that normally sits behind "+N fler" is reachable.
                districts.forEach(function (d) {
                    var hit = districtMatches.indexOf(d) !== -1;
                    d.hidden = !hit;
                    d.classList.remove("is-extra");
                    label(d, hit ? needle : "");
                });
                if (more) more.hidden = true;
            }
        });

        groups.forEach(function (group) {
            var visible = Array.prototype.slice
                .call(group.querySelectorAll("[data-area-card]"))
                .filter(function (card) {
                    return !card.hidden;
                });
            group.hidden = visible.length === 0;
            var meta = group.querySelector("[data-group-count]");
            if (meta) {
                meta.textContent =
                    visible.length + (visible.length === 1 ? " kommun" : " kommuner");
            }
        });

        if (emptyEl) emptyEl.hidden = shown !== 0;
        if (termEl && needle) termEl.textContent = '"' + query.trim() + '"';
        if (countEl) {
            countEl.textContent = needle
                ? shown + (shown === 1 ? " kommun matchar" : " kommuner matchar")
                : "";
        }
    }

    input.addEventListener("input", function (event) {
        apply(event.target.value);
    });

    // Escape clears, which is what a search field is expected to do.
    input.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            input.value = "";
            apply("");
        }
    });
})();
