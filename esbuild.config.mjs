import * as esbuild from "esbuild";

const isWatch = process.argv.includes("--watch");

// Tiptap rich-text editor - IIFE (loaded via a regular <script> tag, exposed
// as window.AdxTiptap). Only loaded in /manage/, never on the public site.
const tiptapConfig = {
  entryPoints: ["src/js/tiptap-editor.js"],
  bundle: true,
  outfile: "static/js/dist/tiptap-editor.js",
  format: "iife",
  globalName: "AdxTiptap",
  target: ["es2020"],
  minify: !isWatch,
  sourcemap: isWatch,
  logLevel: "info",
};

if (isWatch) {
  const ctx = await esbuild.context(tiptapConfig);
  await ctx.watch();
  console.log("Watching for changes...");
} else {
  await esbuild.build(tiptapConfig);
}
