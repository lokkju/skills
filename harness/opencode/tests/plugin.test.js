// Loads the OpenCode entry the way OpenCode does (default export { id, server }),
// runs its hooks against a fake config and fake tool output, and checks what it adds.
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import plugin from "../index.js";
import { parseCommand } from "../lib.js";

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
