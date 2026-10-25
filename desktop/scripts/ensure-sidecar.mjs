// Pre-tauri hook for the bundled local backend.
//
// Production installers contain the backend. Cloud-only builds talk to a deployed backend (a Hugging
// Face Space — Settings → Backend) when DOBOT_CLOUD_ONLY=1 is set.
//
// A local `build` stages backend/dist/dobot-backend.exe to
// src-tauri/binaries/dobot-backend-<triple>.exe (the triple suffix is how Tauri matches an
// `externalBin`) and tauri.mjs injects tauri.sidecar.json into the bundle config. A missing backend
// hard-fails that build — the mistake this project actually hit: an installer whose bundled backend
// was absent.
//
// Build the real sidecar first with scripts\build-backend-exe.bat or `npm run sidecar` (~1-2 min).
import { copyFileSync, existsSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url)); // desktop/scripts
const desktop = join(here, "..");
const repoRoot = join(desktop, "..");
const args = process.argv.slice(2);
const building = args.includes("build");
const bundling =
  process.env.DOBOT_CLOUD_ONLY !== "1" &&
  (process.env.DOBOT_BUNDLE_SIDECAR === "1" || building);
// Tauri sets TAURI_ENV_TARGET_TRIPLE when it runs hooks; fall back to the host triple this repo
// targets (Windows x64) so staging also works when the script is run by hand.
const triple = process.env.TAURI_ENV_TARGET_TRIPLE || "x86_64-pc-windows-msvc";
const exeSuffix = triple.includes("windows") ? ".exe" : "";
const source = join(repoRoot, "backend", "dist", "dobot-backend" + exeSuffix);
const binariesDir = join(desktop, "src-tauri", "binaries");
const target = join(binariesDir, "dobot-backend" + (triple ? `-${triple}` : "") + exeSuffix);

function removeStale() {
  if (!existsSync(binariesDir)) return;
  for (const entry of readdirSync(binariesDir)) {
    if (entry.startsWith("dobot-backend")) {
      rmSync(join(binariesDir, entry));
      console.log(`sidecar: removed stale ${entry}`);
    }
  }
}

if (!bundling) {
  // Cloud-only builds must not accidentally pick up a sidecar left by a previous local build.
  removeStale();
  if (building) {
    console.log("sidecar: cloud-only installer — the app uses an external backend");
  }
} else if (existsSync(source)) {
  mkdirSync(binariesDir, { recursive: true });
  copyFileSync(source, target);
  console.log(
    `sidecar: bundling backend ${(statSync(target).size / 1_048_576).toFixed(1)} MB -> ${target}`,
  );
} else if (building) {
  console.error(
    [
      "sidecar: local installer requires backend/dist/dobot-backend.exe, but it is missing.",
      "",
      "Build it first (one command, ~1-2 min):",
      "    scripts\\build-backend-exe.bat",
      "",
      "Use `DOBOT_CLOUD_ONLY=1 npm run tauri build` for a thin external-backend installer.",
    ].join("\n"),
  );
  process.exit(1);
} else {
  // Dev/info with the flag set but no real exe: stage a placeholder so cargo can compile. Dev
  // builds never spawn it (cfg!(debug_assertions) in backend.rs).
  mkdirSync(binariesDir, { recursive: true });
  writeFileSync(target, "placeholder — dev builds do not spawn the bundled backend\n");
  console.log(`sidecar: placeholder staged for dev -> ${target}`);
}
