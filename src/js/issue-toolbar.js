/**
 * Verktygsraden för ärendebeskrivningar (data-tiptap="issue"), i kundportalen
 * och i tavlans panel. Grupperna, i den ordning Giovanni angav:
 *
 *   Ångra / Gör om
 *   Fet, genomstruken, markering, länk
 *   Tabell, listor
 *   Textjustering
 *
 * Står markören i en tabell visas en andra rad med tabellens egna knappar
 * (rad/kolumn före/efter, ta bort). Ikonerna är inline-SVG (Lucide-stil),
 * inga typsnitt eller bilder att ladda.
 */

const I = (d) =>
  `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;

const ICONS = {
  undo: I('<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/>'),
  redo: I('<path d="M21 7v6h-6"/><path d="M3 17a9 9 0 0 1 15-6.7L21 13"/>'),
  bold: I('<path d="M6 4h8a4 4 0 0 1 0 8H6z"/><path d="M6 12h9a4 4 0 0 1 0 8H6z"/>'),
  strike: I('<path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><line x1="4" y1="12" x2="20" y2="12"/>'),
  mark: I('<path d="m9 11-6 6v3h9l3-3"/><path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4"/>'),
  link: I('<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'),
  table: I('<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/>'),
  ul: I('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1" fill="currentColor"/><circle cx="4" cy="12" r="1" fill="currentColor"/><circle cx="4" cy="18" r="1" fill="currentColor"/>'),
  ol: I('<line x1="10" y1="6" x2="21" y2="6"/><line x1="10" y1="12" x2="21" y2="12"/><line x1="10" y1="18" x2="21" y2="18"/><path d="M4 6h1v4"/><path d="M4 10h2"/><path d="M6 18H4c0-1 2-2 2-3s-1-1.5-2-1"/>'),
  left: I('<line x1="21" y1="6" x2="3" y2="6"/><line x1="15" y1="12" x2="3" y2="12"/><line x1="17" y1="18" x2="3" y2="18"/>'),
  center: I('<line x1="21" y1="6" x2="3" y2="6"/><line x1="17" y1="12" x2="7" y2="12"/><line x1="19" y1="18" x2="5" y2="18"/>'),
  right: I('<line x1="21" y1="6" x2="3" y2="6"/><line x1="21" y1="12" x2="9" y2="12"/><line x1="21" y1="18" x2="7" y2="18"/>'),
};

const btn = (cmd, title, icon, extra = "") =>
  `<button type="button" class="tiptap-tb__btn" data-cmd="${cmd}" title="${title}" aria-label="${title}" ${extra}>${ICONS[icon]}</button>`;
const sep = '<span class="tiptap-tb__sep"></span>';

export function createIssueToolbar(editor, mount) {
  const toolbar = document.createElement("div");
  toolbar.className = "tiptap-tb tiptap-tb--issue";
  toolbar.setAttribute("role", "toolbar");
  toolbar.setAttribute("aria-label", "Textformatering");
  toolbar.innerHTML = [
    btn("undo", "Ångra (⌘Z)", "undo"),
    btn("redo", "Gör om (⇧⌘Z)", "redo"),
    sep,
    btn("toggleBold", "Fet (⌘B)", "bold"),
    btn("toggleStrike", "Genomstruken (⇧⌘S)", "strike"),
    btn("toggleHighlight", "Markering (⇧⌘H)", "mark"),
    btn("link", "Länk (⌘K)", "link"),
    sep,
    btn("insertTable", "Tabell", "table"),
    btn("toggleBulletList", "Punktlista", "ul"),
    btn("toggleOrderedList", "Numrerad lista", "ol"),
    sep,
    btn("alignLeft", "Vänsterställ", "left"),
    btn("alignCenter", "Centrera", "center"),
    btn("alignRight", "Högerställ", "right"),
  ].join("");

  const tableBar = document.createElement("div");
  tableBar.className = "tiptap-tb tiptap-tb--table";
  tableBar.hidden = true;
  tableBar.innerHTML = [
    '<span class="tiptap-tb__label">Tabell</span>',
    text("addRowAfter", "Rad under"),
    text("addRowBefore", "Rad över"),
    text("addColumnAfter", "Kolumn höger"),
    text("addColumnBefore", "Kolumn vänster"),
    text("toggleHeaderRow", "Rubrikrad"),
    sep,
    text("deleteRow", "Ta bort rad"),
    text("deleteColumn", "Ta bort kolumn"),
    text("deleteTable", "Ta bort tabell"),
  ].join("");

  function text(cmd, label) {
    return `<button type="button" class="tiptap-tb__btn tiptap-tb__btn--text" data-cmd="${cmd}">${label}</button>`;
  }

  const ALIGN = { alignLeft: "left", alignCenter: "center", alignRight: "right" };

  function run(cmd) {
    const chain = editor.chain().focus();
    if (cmd === "link") {
      const prev = editor.getAttributes("link").href || "";
      const url = window.prompt("Länkadress (https://, mailto: eller tel:):", prev);
      if (url === null) return;
      if (url.trim() === "") chain.unsetLink().run();
      else chain.extendMarkRange("link").setLink({ href: url.trim() }).run();
      return;
    }
    if (cmd in ALIGN) {
      // Vänster är standard: att "vänsterställa" är att ta bort justeringen,
      // så att det sparade HTML:et inte får style-attribut i onödan.
      if (ALIGN[cmd] === "left") chain.unsetTextAlign().run();
      else chain.setTextAlign(ALIGN[cmd]).run();
      return;
    }
    if (cmd === "insertTable") {
      chain.insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run();
      return;
    }
    if (typeof chain[cmd] === "function") chain[cmd]().run();
  }

  const onClick = (e) => {
    const b = e.target.closest(".tiptap-tb__btn");
    if (!b) return;
    e.preventDefault();
    run(b.dataset.cmd);
    sync();
  };
  toolbar.addEventListener("click", onClick);
  tableBar.addEventListener("click", onClick);

  function sync() {
    const active = {
      toggleBold: editor.isActive("bold"),
      toggleStrike: editor.isActive("strike"),
      toggleHighlight: editor.isActive("highlight"),
      link: editor.isActive("link"),
      insertTable: editor.isActive("table"),
      toggleBulletList: editor.isActive("bulletList"),
      toggleOrderedList: editor.isActive("orderedList"),
      alignCenter: editor.isActive({ textAlign: "center" }),
      alignRight: editor.isActive({ textAlign: "right" }),
    };
    active.alignLeft = !active.alignCenter && !active.alignRight;
    toolbar.querySelectorAll("[data-cmd]").forEach((b) => {
      const cmd = b.dataset.cmd;
      if (cmd in active) b.classList.toggle("is-active", active[cmd]);
      if (cmd === "undo") b.disabled = !editor.can().undo();
      if (cmd === "redo") b.disabled = !editor.can().redo();
    });
    tableBar.hidden = !editor.isActive("table");
  }
  editor.on("selectionUpdate", sync);
  editor.on("transaction", sync);

  mount.appendChild(toolbar);
  mount.appendChild(tableBar);
  sync();
  return { destroy: () => { toolbar.remove(); tableBar.remove(); } };
}
