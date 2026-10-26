// OpenCode entry for lokkju/skills (V1 plugin API, OpenCode 1.19.4 and later 1.x).
//
// Registers each enabled plugin's skills (plugins/<p>/skills on skills.paths) and
// commands (plugins/<p>/commands/*.md, ${CLAUDE_PLUGIN_ROOT} resolved), and fills in
// ${CLAUDE_SKILL_DIR} when a skill is loaded or a file inside one is read.
//
// Options, from opencode.json's "plugin": [["@lokkju/skills@...", { ... }]]:
//   plugins: ["forge-tracker", ...]  which plugins to enable; all when unset
//   allowScripts: true                add bash allow rules for the skills' own scripts
import path from "node:path";
import { fileURLToPath } from "node:url";
import { applyConfig, discoverPlugins, selectPlugins, skillDirFor, substitute } from "./lib.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

async function server(_input, options = {}) {
  const plugins = selectPlugins(discoverPlugins(ROOT), options.plugins);
  const allowScripts = options.allowScripts === true;

  return {
    config: async (config) => {
      applyConfig(config, plugins, { allowScripts });
    },
    "tool.execute.after": async (input, output) => {
      if (typeof output?.output !== "string" || !output.output.includes("${CLAUDE_")) return;
      let hit;
      if (input.tool === "skill") {
        const dir = output.metadata?.dir ?? baseDirectory(output.output);
        hit = skillDirFor(plugins, dir) ?? (dir ? { dir } : undefined);
      } else if (input.tool === "read") {
        hit = skillDirFor(plugins, input.args?.filePath);
      }
      if (!hit) return;
      const vars = { CLAUDE_SKILL_DIR: hit.dir };
      if (hit.plugin) vars.CLAUDE_PLUGIN_ROOT = hit.plugin.dir;
      output.output = substitute(output.output, vars);
    },
  };
}

// OpenCode's skill tool ends its output with this line; used when metadata lacks dir.
function baseDirectory(text) {
  return /^Base directory for this skill: (.+)$/m.exec(text)?.[1]?.trim();
}

export default { id: "lokkju-skills", server };
