/**
 * damage-control para Pi: reutiliza el hook de Claude Code (claude/hooks/damage-control.py), así que las
 * reglas (y las locales de ~/.config/dotfiles-ia/damage-control.local.json) viven en un solo sitio.
 * Bloquear → se bloquea. Preguntar → confirmación si hay UI; sin UI (pi -p) se bloquea.
 */
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const HOOK = process.env.DAMAGE_CONTROL_HOOK ?? join(homedir(), ".claude", "hooks", "damage-control.py");

function entradaClaude(tool: string, input: Record<string, unknown>) {
	if (tool === "bash") return { tool_name: "Bash", tool_input: { command: input.command } };
	if (tool === "write") return { tool_name: "Write", tool_input: { file_path: input.path } };
	if (tool === "edit") return { tool_name: "Edit", tool_input: { file_path: input.path } };
	return null;
}

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event, ctx) => {
		if (process.env.DAMAGE_CONTROL === "0") return undefined;
		const entrada = entradaClaude(event.toolName, event.input as Record<string, unknown>);
		if (!entrada) return undefined;

		const r = spawnSync("python3", [HOOK], { input: JSON.stringify(entrada), encoding: "utf8" });
		if (r.error) return { block: true, reason: `damage-control no pudo ejecutarse: ${r.error.message}` };
		if (r.status === 2) return { block: true, reason: (r.stderr || "bloqueado por damage-control").trim() };

		let motivo = "";
		try {
			motivo = JSON.parse(r.stdout || "{}")?.hookSpecificOutput?.permissionDecisionReason ?? "";
		} catch {
			/* sin decisión: se permite */
		}
		if (!motivo) return undefined;
		if (ctx.hasUI && (await ctx.ui.confirm("¿Permitir?", motivo))) return undefined;
		return { block: true, reason: motivo };
	});
}
