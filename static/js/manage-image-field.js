// Media picker: a visual thumbnail grid drives a hidden FK input + a live preview.
// Reusable - works anywhere [data-image-field] appears. Idempotent + re-runnable
// so dynamically added rows (block editor lists) get wired up too.
(function () {
    function bind(field) {
        if (field.dataset.imageBound === "1") return;
        field.dataset.imageBound = "1";

        const hidden = field.querySelector("input[type=hidden]");
        const previewBox = field.querySelector(".m-image-field__preview");
        const picker = field.querySelector("[data-media-picker]");
        if (!hidden || !previewBox || !picker) return;

        const toggle = picker.querySelector("[data-media-picker-toggle]");
        const panel = picker.querySelector("[data-media-picker-panel]");
        const items = picker.querySelectorAll("[data-media-id]");

        // Toggle panel open/closed
        toggle.addEventListener("click", () => {
            const isOpen = !panel.hidden;
            panel.hidden = isOpen;
            toggle.textContent = isOpen ? "Välj bild" : "Stäng";
        });

        // Handle item selection
        items.forEach((item) => {
            item.addEventListener("click", () => {
                const id = item.dataset.mediaId;
                const url = item.dataset.mediaUrl;
                hidden.value = id;

                // Update preview
                if (id && url) {
                    previewBox.innerHTML =
                        '<img data-image-preview src="' + url + '" alt="">';
                } else {
                    previewBox.innerHTML =
                        '<div class="m-image-field__empty" data-image-preview-empty>Ingen bild vald</div>';
                }

                // Update selection state
                items.forEach((i) => i.classList.remove("is-selected"));
                item.classList.add("is-selected");

                // Close panel
                panel.hidden = true;
                toggle.textContent = "Välj bild";
            });
        });

        // Also support the old <select> if present (backwards compat for block editor)
        const select = field.querySelector("[data-image-select]");
        if (select) {
            select.addEventListener("change", () => {
                const opt = select.selectedOptions[0];
                const id = select.value;
                const url = opt ? opt.dataset.url : "";
                hidden.value = id;
                if (id && url) {
                    previewBox.innerHTML =
                        '<img data-image-preview src="' + url + '" alt="">';
                } else {
                    previewBox.innerHTML =
                        '<div class="m-image-field__empty" data-image-preview-empty>Ingen bild vald</div>';
                }
            });
        }
    }

    function init(root) {
        (root || document).querySelectorAll("[data-image-field]").forEach(bind);
    }

    window.MImageField = { init };
    init();
})();
