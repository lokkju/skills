// Loads the OpenCode entry the way OpenCode does (default export { id, server }),
// runs its hooks against a fake config and fake tool output, and checks what it adds.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import plugin from "../index.js";
import { ASK_TOOLS, SCRIPT_TOOLS, parseCommand } from "../lib.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const PLUGINS = path.join(ROOT, "plugins");
const withSkills = readdirSync(PLUGINS).filter((p) =>
  readdirSync(path.join(PLUGINS, p)).includes("skills"),
);

async function load(options) {
  const hooks = await plugin.server({ directory: ROOT, worktree: ROOT }, options);
  const config = {};
  await hooks.config(config);
  return { hooks, config };
}

test("default export is a V1 plugin module", () => {
  assert.equal(plugin.id, "lokkju-skills");
  assert.equal(typeof plugin.server, "function");
});

test("adds every plugin's skills directory to skills.paths", async () => {
  const { config } = await load();
  assert.deepEqual(
    [...config.skills.paths].sort(),
    withSkills.map((p) => path.join(PLUGINS, p, "skills")).sort(),
  );
});

test("keeps paths and commands the user already has", async () => {
  const hooks = await plugin.server({}, {});
  const config = { skills: { paths: ["/mine"] }, command: { next: { template: "mine" } } };
  await hooks.config(config);
  assert.equal(config.skills.paths[0], "/mine");
  assert.deepEqual(config.command.next, { template: "mine" });
  assert.ok(config.command.catchup);
});

test("registers commands with CLAUDE_PLUGIN_ROOT resolved", async () => {
  const { config } = await load();
  const root = path.join(PLUGINS, "forge-tracker");
  for (const name of ["catchup", "next", "groom"]) {
    const cmd = config.command[name];
    assert.ok(cmd, `missing /${name}`);
    assert.ok(cmd.description.length > 0);
    assert.ok(!cmd.template.includes("${CLAUDE_PLUGIN_ROOT}"));
    assert.ok(cmd.template.includes(`"${root}/skills/forge-tracker/scripts/forge-tracker"`));
    assert.ok(cmd.template.includes("$ARGUMENTS"));
    assert.ok(!cmd.template.startsWith("---"));
    assert.equal(cmd["argument-hint"], undefined);
  }
});

test("the plugins option limits what's registered", async () => {
  const { config } = await load({ plugins: ["term-recorder"] });
  assert.deepEqual(config.skills.paths, [path.join(PLUGINS, "term-recorder", "skills")]);
  assert.deepEqual(config.command, {});
});

test("an unknown plugin name is an error", async () => {
  await assert.rejects(plugin.server({}, { plugins: ["nope"] }), /unknown plugin nope/);
});

test("no bash rules unless allowScripts is set", async () => {
  const { config } = await load();
  assert.equal(config.permission.bash, undefined);
});

test("adds forge-tracker's MCP server, with its write tools asking", async () => {
  const { config } = await load({ plugins: ["forge-tracker"] });
  const script = path.join(PLUGINS, "forge-tracker", "skills", "forge-tracker", "scripts", "forge-tracker-mcp");
  assert.deepEqual(config.mcp["forge-tracker"], {
    type: "local",
    command: ["python3", script],
    enabled: true,
  });
  assert.deepEqual(config.permission, {
    "forge-tracker_groom_apply": "ask",
    "forge-tracker_link": "ask",
    "forge-tracker_write": "ask",
    "forge-tracker_labels_create": "ask",
  });
});

test("the ask list is the server's own list of write tools", () => {
  const scripts = path.join(PLUGINS, "forge-tracker", "skills", "forge-tracker", "scripts");
  const out = execFileSync(
    "python3",
    ["-c", "import json; from forge_tracker import server; print(json.dumps(server.WRITE_TOOLS))"],
    { cwd: scripts, encoding: "utf8" },
  );
  assert.deepEqual(ASK_TOOLS["forge-tracker"], JSON.parse(out));
});

test("adds term-recorder's MCP server, its record following the bash catch-all", async () => {
  const { config } = await load({ plugins: ["term-recorder"] });
  const script = path.join(PLUGINS, "term-recorder", "skills", "term-recorder", "scripts", "term-recorder-mcp");
  assert.deepEqual(config.mcp["term-recorder"], {
    type: "local",
    command: ["python3", script],
    enabled: true,
  });
  // OpenCode allows bash by default, so record is allowed too.
  assert.equal(config.permission, undefined);

  const hooks = await plugin.server({}, { plugins: ["term-recorder"] });
  for (const bash of ["ask", { "*": "deny", "git *": "allow" }]) {
    const asking = { permission: { bash } };
    await hooks.config(asking);
    assert.equal(asking.permission["term-recorder_record"], typeof bash === "string" ? bash : "deny");
    assert.deepEqual(asking.permission.bash, bash);
  }
  const allowing = { permission: { bash: "allow" } };
  await hooks.config(allowing);
  assert.equal(allowing.permission["term-recorder_record"], undefined);

  const trusting = await plugin.server({}, { plugins: ["term-recorder"], allowScripts: true });
  const trusted = { permission: { bash: "ask" } };
  await trusting.config(trusted);
  assert.equal(trusted.permission["term-recorder_record"], undefined);
});

test("the script-tool list is the server's own list", () => {
  const scripts = path.join(PLUGINS, "term-recorder", "skills", "term-recorder", "scripts");
  const out = execFileSync(
    "python3",
    ["-c", "import json; from term_recorder import server; print(json.dumps(server.ASK_TOOLS))"],
    { cwd: scripts, encoding: "utf8" },
  );
  assert.deepEqual(SCRIPT_TOOLS["term-recorder"], JSON.parse(out));
});

test("a catch-all stays first and the user's own rules and servers win", async () => {
  const hooks = await plugin.server({}, { plugins: ["forge-tracker"] });
  const config = { permission: { "forge-tracker_link": "allow", "*": "allow" }, mcp: {} };
  await hooks.config(config);
  const keys = Object.keys(config.permission);
  assert.equal(keys[0], "*");
  assert.equal(config.permission["forge-tracker_link"], "allow");
  assert.equal(config.permission["forge-tracker_write"], "ask");

  const mine = { mcp: { "forge-tracker": { type: "local", command: ["mine"] } } };
  await hooks.config(mine);
  assert.deepEqual(mine.mcp["forge-tracker"].command, ["mine"]);
  assert.equal(mine.permission, undefined);
});

test("mcp: false adds no servers", async () => {
  const { config } = await load({ mcp: false });
  assert.equal(config.mcp, undefined);
  assert.equal(config.permission, undefined);
});

test("allowScripts adds bare and quoted rules for each script, after the user's", async () => {
  const hooks = await plugin.server({}, { plugins: ["term-recorder"], allowScripts: true });
  const config = { permission: { bash: "ask" } };
  await hooks.config(config);
  const script = path.join(PLUGINS, "term-recorder", "skills", "term-recorder", "scripts", "term-record");
  const keys = Object.keys(config.permission.bash);
  assert.equal(keys[0], "*");
  assert.equal(config.permission.bash["*"], "ask");
  assert.equal(config.permission.bash[`${script} *`], "allow");
  assert.equal(config.permission.bash[`"${script}" *`], "allow");
});

test("fills in CLAUDE_SKILL_DIR when the skill tool loads a skill", async () => {
  const { hooks } = await load();
  const dir = path.join(PLUGINS, "term-recorder", "skills", "term-recorder");
  const output = {
    title: "Loaded skill: term-recorder",
    output: 'Run "${CLAUDE_SKILL_DIR}/scripts/term-record" -s steps\n\nBase directory for this skill: ' + dir,
    metadata: { name: "term-recorder", dir },
  };
  await hooks["tool.execute.after"]({ tool: "skill", sessionID: "s", callID: "c", args: {} }, output);
  assert.equal(output.output.split("\n")[0], `Run "${dir}/scripts/term-record" -s steps`);
});

test("falls back to the base-directory line when metadata has no dir", async () => {
  const { hooks } = await load();
  const output = { output: "${CLAUDE_SKILL_DIR}/x\nBase directory for this skill: /else/where", metadata: {} };
  await hooks["tool.execute.after"]({ tool: "skill", args: {} }, output);
  assert.ok(output.output.startsWith("/else/where/x"));
});

test("fills in CLAUDE_SKILL_DIR in a file read from inside a skill", async () => {
  const { hooks } = await load();
  const dir = path.join(PLUGINS, "forge-release", "skills", "forge-release");
  const output = { output: "1: ${CLAUDE_SKILL_DIR}/scripts/github-app-create", metadata: {} };
  await hooks["tool.execute.after"](
    { tool: "read", args: { filePath: path.join(dir, "references", "yeet-setup.md") } },
    output,
  );
  assert.equal(output.output, `1: ${dir}/scripts/github-app-create`);
});

test("fills in CLAUDE_SKILL_DIR when a skill runs as a /<skill> command", async () => {
  const { hooks } = await load();
  const dir = path.join(PLUGINS, "worktree-tidy", "skills", "worktree-tidy");
  const body = '"${CLAUDE_SKILL_DIR}/scripts/worktree-tidy" report\n\nBase directory for this skill: ' + dir;
  const output = {
    parts: [
      { type: "text", text: body },
      { type: "subtask", prompt: body },
      { type: "text", text: "${CLAUDE_SKILL_DIR} with no base line" },
    ],
  };
  await hooks["command.execute.before"]({ command: "worktree-tidy", sessionID: "s", arguments: "" }, output);
  assert.ok(output.parts[0].text.startsWith(`"${dir}/scripts/worktree-tidy" report`));
  assert.ok(output.parts[1].prompt.startsWith(`"${dir}/scripts/worktree-tidy" report`));
  assert.equal(output.parts[2].text, "${CLAUDE_SKILL_DIR} with no base line");
});

test("leaves other tools and other files alone", async () => {
  const { hooks } = await load();
  const output = { output: "${CLAUDE_SKILL_DIR}", metadata: {} };
  await hooks["tool.execute.after"]({ tool: "read", args: { filePath: "/tmp/elsewhere.md" } }, output);
  await hooks["tool.execute.after"]({ tool: "bash", args: {} }, output);
  assert.equal(output.output, "${CLAUDE_SKILL_DIR}");
});

test("parseCommand reads quoted and plain front matter", () => {
  const { data, body } = parseCommand('---\ndescription: "a: b"\nsubtask: true\nx: plain\n---\n\nBody\n');
  assert.deepEqual(data, { description: "a: b", subtask: true, x: "plain" });
  assert.equal(body, "Body");
});
