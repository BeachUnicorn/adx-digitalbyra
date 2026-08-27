/**
 * Generic add/remove for repeating form rows in /manage/.
 *
 * Markup contract:
 *   <button data-add-row="<list-id>" data-template="<template-id>">
 *   <button data-remove-row>  (anywhere inside the row)
 *
 * New rows get a unique uid written into any `*_uid` hidden input and into the
 * matching checkbox's value. Rows are keyed by uid rather than by position
 * because unchecked checkboxes don't post at all - a positional scheme
 * misaligns the moment a row above is removed.
 */
(function () {
    "use strict";

    var counter = 0;

    function nextUid() {
        counter += 1;
        return "new-" + counter;
    }

    function prepare(row) {
        var uid = nextUid();
        row.querySelectorAll('input[name$="_uid"]').forEach(function (input) {
            input.value = uid;
        });
        // Pair the row's "active" checkbox with the same uid so the server can
        // tell which rows were ticked.
        row.querySelectorAll('input[type="checkbox"][value=""]').forEach(function (box) {
            box.value = uid;
        });
    }

    document.addEventListener("click", function (event) {
        var addBtn = event.target.closest("[data-add-row]");
        if (addBtn) {
            var list = document.getElementById(addBtn.dataset.addRow);
            var template = document.getElementById(addBtn.dataset.template);
            if (!list || !template) return;
            var fragment = template.content.cloneNode(true);
            var row = fragment.firstElementChild;
            prepare(row);
            list.appendChild(fragment);
            var firstInput = row.querySelector('input[type="text"], textarea');
            if (firstInput) firstInput.focus();
            return;
        }

        var removeBtn = event.target.closest("[data-remove-row]");
        if (removeBtn) {
            var container = removeBtn.closest(".m-repeat-row, .m-repeat-block");
            if (container) container.remove();
        }
    });

    /**
     * Locate the per-option rows inside a rendered checkbox list.
     *
     * Django's CheckboxSelectMultiple wraps each option in a <div> (it used
     * <li> before 4.0), so this finds whichever element actually holds all the
     * inputs and treats its children as the rows. That keeps the filter working
     * regardless of which widget template is in play.
     */
    function optionRows(list) {
        var inputs = Array.prototype.slice.call(
            list.querySelectorAll('input[type="checkbox"]')
        );
        if (!inputs.length) return [];

        var container = inputs[0].parentElement;
        while (
            container &&
            container !== list &&
            container.querySelectorAll('input[type="checkbox"]').length < inputs.length
        ) {
            container = container.parentElement;
        }
        if (!container) container = list;

        return inputs.map(function (input) {
            var node = input;
            while (node.parentElement && node.parentElement !== container) {
                node = node.parentElement;
            }
            return node;
        });
    }

    /**
     * Type-to-filter for long checkbox lists (grannområden runs to 200+).
     * Filtering is visual only - a hidden input still posts, so ticked rows are
     * kept visible to make sure a filter can never hide a choice already made.
     */
    document.addEventListener("input", function (event) {
        var box = event.target.closest("[data-filter-list]");
        if (!box) return;
        var list = document.getElementById(box.dataset.filterList);
        if (!list) return;

        var needle = box.value.trim().toLowerCase();
        var shown = 0;
        optionRows(list).forEach(function (row) {
            var match = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1;
            var ticked = !!row.querySelector("input:checked");
            row.hidden = !(match || ticked);
            if (!row.hidden) shown += 1;
        });

        var counter = document.querySelector(
            '[data-filter-count="' + box.dataset.filterList + '"]'
        );
        if (counter) counter.textContent = needle ? shown + " träffar" : "";
    });
})();
