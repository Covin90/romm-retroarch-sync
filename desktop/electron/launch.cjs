// Launcher for the Electron shell — use this instead of calling `electron .`
// directly from an npm script.
//
// Why it exists: VS Code, Cursor and other Electron-based editors export
// ELECTRON_RUN_AS_NODE=1 into their integrated terminals, and every process
// launched from there inherits it. That variable makes the `electron` binary
// boot as a plain Node process, so `require("electron")` in main.cjs returns a
// path string instead of the API and the app dies with a confusing
// "Cannot read properties of undefined (reading 'commandLine')".
//
// It has to be cleared in the environment we spawn with: the launcher binary
// reads it before any app code runs, so deleting it inside main.cjs is too late.
//
// Also carries the dev-server env vars, so `--dev` works the same on Windows
// (where `FOO=bar cmd` is not valid shell syntax) as it does on Linux.

const { spawn } = require("child_process");
const electron = require("electron"); // path to the binary, when run under Node

const dev = process.argv.includes("--dev");

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

if (dev) {
  // Point the shell at the Vite dev server instead of the built dist/.
  env.ROMM_URL = env.ROMM_URL ?? "http://127.0.0.1:5173";
  env.ROMM_PORT = env.ROMM_PORT ?? "8723";
}

const child = spawn(electron, [require("path").resolve(__dirname, "..")], {
  env,
  stdio: "inherit",
});

child.on("close", (code) => process.exit(code ?? 0));
