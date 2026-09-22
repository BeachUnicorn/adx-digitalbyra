/**
 * Tiptap rich-text editor for /manage/ forms - and for issue descriptions
 * (data-tiptap="issue") in the customer portal and the board drawer.
 *
 * Progressive enhancement: every <textarea data-tiptap> is replaced by a
 * Tiptap editor. The editor's HTML (with {{ variable }} tokens restored) is
 * mirrored back into the hidden textarea on each change, so a normal form
 * POST submits the content - no custom save endpoint needed here.
 *
 * Storage format is plain HTML containing `{{ variable }}` tokens, which the
 * server sanitizes + substitutes via render_with_context_rich. The bundle is
 * built by esbuild into static/js/dist/tiptap-editor.js and loaded only in
 * /manage/.
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import Placeholder from "@tiptap/extension-placeholder";
import TextAlign from "@tiptap/extension-text-align";
import Highlight from "@tiptap/extension-highlight";
import Table from "@tiptap/extension-table";
import TableRow from "@tiptap/extension-table-row";
import TableCell from "@tiptap/extension-table-cell";
import TableHeader from "@tiptap/extension-table-header";
import { VariablePill } from "./extensions/variable-pill.js";
import { createToolbar } from "./toolbar.js";
import { createIssueToolbar } from "./issue-toolbar.js";

// Mirrors render_context.AVAILABLE_VARIABLES on the server.
const CONTEXT_VARIABLES = [
  { key: "site_name", label: "Företagsnamn" },
  { key: "phone", label: "Telefon" },
  { key: "email", label: "E-post" },
  { key: "street_address", label: "Gatuadress" },
  { key: "postal_code", label: "Postnummer" },
  { key: "city", label: "Ort" },
  { key: "full_address", label: "Fullständig adress" },
  { key: "org_number", label: "Org.nummer" },
  { key: "current_year", label: "Årtal" },
];

function htmlToTiptap(html) {
  if (!html) return "<p></p>";
  return html.replace(
    /\{\{\s*([a-z_]+)\s*\}\}/g,
    '<variable-pill data-variable="$1"></variable-pill>'
  );
}

function tiptapToHtml(html) {
  if (!html) return "";
  return html
    .replace(
      /<variable-pill[^>]*data-variable="([^"]+)"[^>]*><\/variable-pill>/g,
      "{{ $1 }}"
    );
}

/** Plain text (one paragraph per blank line, <br> per newline) -> HTML. */
function textToHtml(text) {
  const esc = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const paragraphs = String(text || "").replace(/\r/g, "").trim().split(/\n{2,}/);
  return paragraphs.map((p) => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");
}

/**
 * Ärendebeskrivningen: ingen variabelmeny, ingen rubrik-/citatnivå, men
 * tabeller, markering och justering. Sparas som HTML som servern sanerar
 * (apps/projects/richtext.py). Editorn läggs på textarean som
 * textarea.tiptapEditor så att portal.js kan fylla i exempeltexter.
 */
function enhanceIssue(textarea) {
  const wrapper = document.createElement("div");
  wrapper.className = "tiptap-field tiptap-field--issue";
  textarea.parentNode.insertBefore(wrapper, textarea);
  textarea.hidden = true;
  const editorMount = document.createElement("div");
  editorMount.className = "tiptap-editor";

  const editor = new Editor({
    element: editorMount,
    extensions: [
      StarterKit.configure({
        heading: false,
        blockquote: false,
        codeBlock: false,
        code: false,
        horizontalRule: false,
      }),
      Link.configure({
        openOnClick: false,
        HTMLAttributes: { rel: "noopener noreferrer", target: "_blank" },
        validate: (href) => /^(https?:|mailto:|tel:)/i.test(href.trim()),
      }),
      Highlight,
      TextAlign.configure({ types: ["paragraph"], alignments: ["left", "center", "right"] }),
      Table.configure({ resizable: false }),
      TableRow,
      TableHeader,
      TableCell,
      Placeholder.configure({ placeholder: textarea.getAttribute("placeholder") || "Skriv här..." }),
    ],
    content: textarea.value || "<p></p>",
    onUpdate: ({ editor }) => {
      textarea.value = editor.isEmpty ? "" : editor.getHTML();
    },
    // Tavlans panel sparar på focusout av fältet; textarean är dold, så
    // editorns blur skickas vidare som ett focusout på den.
    onBlur: () => {
      textarea.value = editor.isEmpty ? "" : editor.getHTML();
      textarea.dispatchEvent(new Event("focusout", { bubbles: true }));
    },
  });
  textarea.tiptapEditor = editor;
  textarea.tiptapSetText = (text) => editor.commands.setContent(textToHtml(text), true);

  createIssueToolbar(editor, wrapper);
  wrapper.appendChild(editorMount);
  const form = textarea.closest("form");
  if (form) form.addEventListener("submit", () => { textarea.value = editor.isEmpty ? "" : editor.getHTML(); });
}

function enhance(textarea) {
  if (textarea.dataset.tiptapReady === "1") return;
  textarea.dataset.tiptapReady = "1";
  if (textarea.dataset.tiptap === "issue") return enhanceIssue(textarea);

  // "basic" mode restricts formatting to bold/italic/link + variables.
  const basic = textarea.dataset.tiptap === "basic";

  // Wrapper holds toolbar + editor; the textarea is hidden but kept in the DOM
  // so its value is submitted with the form.
  const wrapper = document.createElement("div");
  wrapper.className = "tiptap-field";
  textarea.parentNode.insertBefore(wrapper, textarea);
  textarea.hidden = true;

  const editorMount = document.createElement("div");
  editorMount.className = "tiptap-editor";

  const starterKit = basic
    ? StarterKit.configure({
        heading: false,
        blockquote: false,
        bulletList: false,
        orderedList: false,
        listItem: false,
        codeBlock: false,
        code: false,
        horizontalRule: false,
        strike: false,
      })
    : StarterKit.configure({ heading: { levels: [2, 3, 4] } });

  const editor = new Editor({
    element: editorMount,
    extensions: [
      starterKit,
      Link.configure({
        openOnClick: false,
        HTMLAttributes: { rel: "noopener noreferrer", target: "_blank" },
        validate: (href) => /^(https?:|mailto:|tel:|#|\/)/i.test(href.trim()),
      }),
      Placeholder.configure({ placeholder: textarea.getAttribute("placeholder") || "Skriv här..." }),
      VariablePill,
    ],
    content: htmlToTiptap(textarea.value),
    onUpdate: ({ editor }) => {
      textarea.value = tiptapToHtml(editor.getHTML());
    },
  });

  createToolbar(editor, wrapper, CONTEXT_VARIABLES, { basic });
  wrapper.appendChild(editorMount);

  // Final sync on submit (covers any pending state).
  const form = textarea.closest("form");
  if (form) {
    form.addEventListener("submit", () => {
      textarea.value = tiptapToHtml(editor.getHTML());
    });
  }
}

function init() {
  document.querySelectorAll("textarea[data-tiptap]").forEach(enhance);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

export { init, htmlToTiptap, tiptapToHtml, textToHtml };
