// Add/remove repeating rows in the block editor (cards, FAQ items, etc.).
// After adding a row, re-initialise Tiptap and image pickers inside it.
(function () {
    document.querySelectorAll("[data-add-row]").forEach((addBtn) => {
        const key = addBtn.getAttribute("data-add-row");
        const list = document.querySelector('[data-rows="' + key + '"]');
        const tpl = document.querySelector('[data-row-template="' + key + '"]');
        if (!list || !tpl) return;

        addBtn.addEventListener("click", () => {
            const frag = tpl.content.cloneNode(true);
            list.appendChild(frag);
            const row = list.lastElementChild;
            if (window.AdxTiptap && window.AdxTiptap.init) window.AdxTiptap.init();
            if (window.MImageField && window.MImageField.init) window.MImageField.init(row);
        });
    });

    document.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-remove-row]");
        if (btn) btn.closest(".m-row-card").remove();

        const moveBtn = e.target.closest("[data-move-row]");
        if (moveBtn) {
            const card = moveBtn.closest(".m-row-card");
            const parent = card.parentNode;
            const dir = moveBtn.getAttribute("data-move-row");
            if (dir === "up" && card.previousElementSibling) {
                parent.insertBefore(card, card.previousElementSibling);
            } else if (dir === "down" && card.nextElementSibling) {
                parent.insertBefore(card.nextElementSibling, card);
            }
        }
    });
})();
