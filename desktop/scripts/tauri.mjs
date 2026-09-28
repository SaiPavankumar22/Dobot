// Wrapper around the tauri CLI so `npm run tauri ...` always runs the sidecar staging hook first.
// Arguments pass through untouched: `npm run tauri build`, `npm run tauri dev`, `npm run tauri info`.
//
// Installers bundle no backend by default (the backend lives on a deployed host — a Hugging Face
// Space — or runs from source). Set DOBOT_BUNDLE_SIDECAR=1 to opt back into the bundled sidecar:
// ensure-sidecar stages the exe and, for `build`, tauri.sidecar.json is merged into the bundle
// config so the bundler knows about `externalBin`.
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const staged = spawnSync(process.execPath, [join(here, "ensure-sidecar.mjs"), ...args], {
  stdio: "inherit",
});
if (staged.status !== 0) process.exit(staged.status ?? 1);

const cli = join(here, "..", "node_modules", "@tauri-apps", "cli", "tauri.js");
const cliArgs = [...args];
if (process.env.DOBOT_BUNDLE_SIDECAR === "1" && args.includes("build")) {
  cliArgs.push("--config", join(here, "..", "src-tauri", "tauri.sidecar.json"));
}
const tauri = spawnSync(process.execPath, [cli, ...cliArgs], { stdio: "inherit" });
process.exit(tauri.status ?? 1);
