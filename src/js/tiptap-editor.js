/**
 * Tiptap rich-text editor for /manage/ forms.
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
import { VariablePill } from "./extensions/variable-pill.js";
import { createToolbar } from "./toolbar.js";

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

function enhance(textarea) {
  if (textarea.dataset.tiptapReady === "1") return;
  textarea.dataset.tiptapReady = "1";

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
      Placeholder.configure({ placeholder: textarea.getAttribute("placeholder") || "Skriv här…" }),
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

export { init, htmlToTiptap, tiptapToHtml };
