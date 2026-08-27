/**
 * Fixed toolbar for the Tiptap editor (rendered above the editor in /manage/).
 *
 * Full mode: bold, italic, bullet/ordered list, quote, link, variable insert.
 * The variable button opens a menu of context variables; picking one inserts
 * a VariablePill node.
 *
 * @param {Editor} editor   Tiptap editor instance
 * @param {HTMLElement} mount  Element to render the toolbar into
 * @param {Array} variables  [{key, label}] context variables
 */
export function createToolbar(editor, mount, variables, opts = {}) {
  const basic = opts.basic || false;
  const toolbar = document.createElement("div");
  toolbar.className = "tiptap-tb";
  toolbar.setAttribute("role", "toolbar");
  toolbar.setAttribute("aria-label", "Textformatering");

  const listButtons = basic
    ? ""
    : `
    <span class="tiptap-tb__sep"></span>
    <button type="button" class="tiptap-tb__btn" data-cmd="toggleBulletList" title="Punktlista" aria-label="Punktlista">•</button>
    <button type="button" class="tiptap-tb__btn" data-cmd="toggleOrderedList" title="Numrerad lista" aria-label="Numrerad lista">1.</button>
    <button type="button" class="tiptap-tb__btn" data-cmd="toggleBlockquote" title="Citat" aria-label="Citat">&rdquo;</button>`;

  toolbar.innerHTML = `
    <button type="button" class="tiptap-tb__btn" data-cmd="toggleBold" title="Fet (⌘B)" aria-label="Fet"><strong>B</strong></button>
    <button type="button" class="tiptap-tb__btn" data-cmd="toggleItalic" title="Kursiv (⌘I)" aria-label="Kursiv"><em>I</em></button>
    ${listButtons}
    <span class="tiptap-tb__sep"></span>
    <button type="button" class="tiptap-tb__btn" data-cmd="toggleLink" title="Länk (⌘K)" aria-label="Länk">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
    </button>
    <button type="button" class="tiptap-tb__btn" data-cmd="unsetLink" title="Ta bort länk" aria-label="Ta bort länk">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/><line x1="4" y1="4" x2="20" y2="20"/></svg>
    </button>
    <span class="tiptap-tb__sep"></span>
    <div class="tiptap-tb__var-wrapper">
      <button type="button" class="tiptap-tb__btn tiptap-tb__btn--var" data-cmd="showVariables" title="Infoga variabel" aria-label="Infoga variabel" aria-haspopup="true">{ }</button>
      <div class="tiptap-tb__var-menu" role="menu" aria-label="Variabler" hidden></div>
    </div>
  `;

  // Build the variable menu
  const varMenu = toolbar.querySelector(".tiptap-tb__var-menu");
  variables.forEach((v) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "tiptap-tb__var-item";
    item.textContent = v.label;
    item.dataset.variable = v.key;
    item.addEventListener("click", () => {
      editor.chain().focus().insertVariable(v.key).run();
      varMenu.hidden = true;
    });
    varMenu.appendChild(item);
  });

  toolbar.addEventListener("click", (e) => {
    const btn = e.target.closest(".tiptap-tb__btn");
    if (!btn) return;
    const cmd = btn.dataset.cmd;
    if (cmd === "showVariables") {
      varMenu.hidden = !varMenu.hidden;
      return;
    }
    if (cmd === "toggleLink") {
      const prev = editor.getAttributes("link").href || "";
      const url = window.prompt("Länkadress (https://, mailto:, tel: eller /sida):", prev);
      if (url === null) return;
      if (url === "") {
        editor.chain().focus().unsetLink().run();
      } else {
        editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
      }
      return;
    }
    editor.chain().focus()[cmd]().run();
    syncActive();
  });

  // Close the variable menu on outside click
  document.addEventListener("click", (e) => {
    if (!toolbar.contains(e.target)) varMenu.hidden = true;
  });

  function syncActive() {
    toolbar.querySelectorAll(".tiptap-tb__btn[data-cmd]").forEach((btn) => {
      const cmd = btn.dataset.cmd;
      const markMap = {
        toggleBold: "bold",
        toggleItalic: "italic",
        toggleBulletList: "bulletList",
        toggleOrderedList: "orderedList",
        toggleBlockquote: "blockquote",
        toggleLink: "link",
      };
      const name = markMap[cmd];
      if (name) btn.classList.toggle("is-active", editor.isActive(name));
    });
  }

  editor.on("selectionUpdate", syncActive);
  editor.on("transaction", syncActive);

  mount.appendChild(toolbar);
  return { destroy: () => toolbar.remove() };
}
