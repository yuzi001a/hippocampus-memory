// Reproducible build: copy the three runtime sources into dist/.
// No bundler, no dependency install, no network — the same inputs always
// produce the same dist bytes, which is what npm run build must guarantee.
import { copyFileSync, mkdirSync, readdirSync, rmSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const srcDir = join(root, "src");
const outDir = join(root, "dist");
const SOURCES = ["index.js", "adapter.js", "bridge.js"];

rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const available = new Set(readdirSync(srcDir).filter((name) => statSync(join(srcDir, name)).isFile()));
for (const name of SOURCES) {
  if (!available.has(name)) throw new Error(`missing runtime source: src/${name}`);
  copyFileSync(join(srcDir, name), join(outDir, name));
}

process.stdout.write(`built dist/: ${SOURCES.join(", ")}\n`);
