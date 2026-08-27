/**
 * Tiptap custom node: VariablePill
 *
 * Renders `{{ variable_name }}` tokens as styled inline pills in the editor.
 * Stored as `<variable-pill data-variable="phone"></variable-pill>` in the
 * editor's internal HTML, then converted back to `{{ phone }}` before saving
 * (see tiptapToHtml in the main module).
 *
 * The pill is atomic (non-editable) - users can select and delete it but
 * can't type inside it. This prevents corruption of variable names.
 */

import { Node, mergeAttributes } from "@tiptap/core";

export const VariablePill = Node.create({
  name: "variablePill",

  group: "inline",
  inline: true,
  atom: true,

  addAttributes() {
    return {
      variable: {
        default: null,
        parseHTML: (element) => element.getAttribute("data-variable"),
        renderHTML: (attributes) => ({
          "data-variable": attributes.variable,
        }),
      },
    };
  },

  parseHTML() {
    return [{ tag: "variable-pill" }];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "variable-pill",
      mergeAttributes(HTMLAttributes, {
        class: "tiptap-variable-pill",
        contenteditable: "false",
      }),
      `{{ ${HTMLAttributes["data-variable"] || ""} }}`,
    ];
  },

  addCommands() {
    return {
      insertVariable:
        (variableKey) =>
        ({ commands }) =>
          commands.insertContent({
            type: this.name,
            attrs: { variable: variableKey },
          }),
    };
  },
});
