// The pure half of the OpenCode entry: find the plugins under plugins/, read their
// commands, and build what the config hook adds. No OpenCode imports, so the tests
// run it under Node with a fake config.
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

// Plugins whose skills and commands this package offers. session-history lives in
// lokkju/claude-session-index and reads Claude Code transcripts, so it isn't here.
// session-decisions has no commands; in OpenCode its skill falls back to the
// labelled list, since its tools, cards and sidebar are a Claude Code mod.
export function discoverPlugins(root) {
  const base = path.join(root, "plugins");
  const found = [];
  for (const name of readdirSync(base).sort()) {
    const dir = path.join(base, name);
    if (!isDir(dir)) continue;
    const skills = path.join(dir, "skills");
    const commands = path.join(dir, "commands");
    const plugin = { name, dir, skills: [], commands: [], scripts: [] };
    if (isDir(skills)) {
      for (const skill of readdirSync(skills).sort()) {
        const skillDir = path.join(skills, skill);
        if (!isFile(path.join(skillDir, "SKILL.md"))) continue;
        plugin.skills.push(skillDir);
        plugin.scripts.push(...executables(path.join(skillDir, "scripts")));
      }
    }
    if (isDir(commands)) {
      for (const file of readdirSync(commands).sort()) {
        if (file.endsWith(".md")) plugin.commands.push(path.join(commands, file));
      }
    }
    if (plugin.skills.length || plugin.commands.length) found.push(plugin);
  }
  return found;
}

// Which plugins to enable, from the `plugins` option. Unset means all of them; an
// unknown name is an error, so a typo doesn't silently turn a plugin off.
export function selectPlugins(available, wanted) {
  if (wanted === undefined || wanted === null) return available;
  if (!Array.isArray(wanted) || wanted.some((n) => typeof n !== "string")) {
    throw new TypeError("lokkju/skills: the `plugins` option must be a list of plugin names");
  }
  const names = new Set(available.map((p) => p.name));
  const unknown = wanted.filter((n) => !names.has(n));
  if (unknown.length) {
    throw new TypeError(
      `lokkju/skills: unknown plugin ${unknown.join(", ")}; available: ${[...names].join(", ")}`,
    );
  }
  return available.filter((p) => wanted.includes(p.name));
}

// Front matter of the shape our command files use: `key: value` lines, values
// plain or quoted. OpenCode's own command keys that we pass on are description,
// agent, model and subtask; argument-hint has no OpenCode field.
export function parseCommand(text) {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
  const data = {};
  let body = text;
  if (match) {
    body = text.slice(match[0].length);
    for (const line of match[1].split(/\r?\n/)) {
      const kv = /^([A-Za-z][\w-]*):\s*(.*)$/.exec(line);
      if (!kv) continue;
      data[kv[1]] = scalar(kv[2].trim());
    }
  }
  return { data, body: body.trim() };
}

function scalar(raw) {
  if (raw.startsWith('"') && raw.endsWith('"') && raw.length >= 2) {
    try {
      return JSON.parse(raw);
    } catch {
      return raw.slice(1, -1);
    }
  }
  if (raw.startsWith("'") && raw.endsWith("'") && raw.length >= 2) {
    return raw.slice(1, -1).replaceAll("''", "'");
  }
  if (raw === "true") return true;
  if (raw === "false") return false;
  return raw;
}

export function substitute(text, vars) {
  let out = text;
  for (const [name, value] of Object.entries(vars)) {
    out = out.replaceAll("${" + name + "}", value);
  }
  return out;
}

const COMMAND_KEYS = ["description", "agent", "model", "subtask"];

export function buildCommands(plugins) {
  const commands = {};
  for (const plugin of plugins) {
    for (const file of plugin.commands) {
      const { data, body } = parseCommand(readFileSync(file, "utf8"));
      const entry = { template: substitute(body, { CLAUDE_PLUGIN_ROOT: plugin.dir }) };
      for (const key of COMMAND_KEYS) {
        if (data[key] !== undefined) entry[key] = data[key];
      }
      commands[path.basename(file, ".md")] = entry;
    }
  }
  return commands;
}

// Bash allow rules for the skills' own scripts, both bare and quoted, since the
// skills write them quoted and OpenCode matches the command text as written.
// A trailing " *" also matches the bare command.
export function scriptRules(plugins) {
  const rules = {};
  for (const plugin of plugins) {
    for (const script of plugin.scripts) {
      rules[`${script} *`] = "allow";
      rules[`"${script}" *`] = "allow";
    }
  }
  return rules;
}

// What the config hook does, on the live config object.
export function applyConfig(config, plugins, { allowScripts = false } = {}) {
  config.skills ??= {};
  config.skills.paths ??= [];
  for (const plugin of plugins) {
    const dir = path.join(plugin.dir, "skills");
    if (plugin.skills.length && !config.skills.paths.includes(dir)) config.skills.paths.push(dir);
  }

  config.command ??= {};
  for (const [name, entry] of Object.entries(buildCommands(plugins))) {
    // A command the user defined keeps its name.
    if (config.command[name] === undefined) config.command[name] = entry;
  }

  if (allowScripts) {
    const rules = scriptRules(plugins);
    if (Object.keys(rules).length) {
      config.permission ??= {};
      const bash = config.permission.bash;
      const existing = typeof bash === "string" ? { "*": bash } : (bash ?? {});
      config.permission.bash = { ...existing, ...rules };
    }
  }
  return config;
}

// The skill directory a path sits in, if it's one of ours.
export function skillDirFor(plugins, file) {
  if (typeof file !== "string") return undefined;
  const resolved = path.resolve(file);
  for (const plugin of plugins) {
    for (const dir of plugin.skills) {
      if (resolved === dir || resolved.startsWith(dir + path.sep)) return { plugin, dir };
    }
  }
  return undefined;
}

function isDir(p) {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function isFile(p) {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
  }
}

function executables(dir) {
  if (!isDir(dir)) return [];
  const out = [];
  for (const name of readdirSync(dir).sort()) {
    const file = path.join(dir, name);
    try {
      const st = statSync(file);
      if (st.isFile() && st.mode & 0o111) out.push(file);
    } catch {
      // unreadable: skip
    }
  }
  return out;
}
