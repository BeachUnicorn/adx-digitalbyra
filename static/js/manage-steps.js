// Add/remove service step rows on the service form.
(function () {
    const list = document.getElementById("steps-list");
    const addBtn = document.getElementById("add-step");
    const tpl = document.getElementById("step-template");
    if (!list || !addBtn || !tpl) return;

    addBtn.addEventListener("click", () => {
        list.appendChild(tpl.content.cloneNode(true));
    });

    list.addEventListener("click", (e) => {
        if (e.target.closest("[data-remove-step]")) {
            e.target.closest(".m-step-row").remove();
        }
    });
})();
