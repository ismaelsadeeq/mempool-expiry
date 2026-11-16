// Rasterize results/*.svg to results/*.png (2x width) for forum/README embedding.
// Requires: npm install @resvg/resvg-js
const { Resvg } = require("@resvg/resvg-js");
const fs = require("fs");

for (const f of fs.readdirSync("results").filter((x) => x.endsWith(".svg"))) {
  const svg = fs.readFileSync("results/" + f);
  const r = new Resvg(svg, {
    fitTo: { mode: "width", value: 1640 },
    font: { loadSystemFonts: true, defaultFontFamily: "sans-serif" },
  });
  const out = "results/" + f.replace(".svg", ".png");
  fs.writeFileSync(out, r.render().asPng());
  console.log(out);
}
