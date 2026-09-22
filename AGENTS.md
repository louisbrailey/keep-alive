# Keep Alive working instructions

Read: README.md, ARCHITECTURE.md, THREAT_MODEL.md, macos-panel/README.md.

The Python service is in src/keepalive; the separate Safari/app control panel is in macos-panel. Preserve observe-only defaults and bounded opt-in recovery. Tests must not restart launchd services, launch apps, send Discord messages or change power settings. Live panel deployment remains ~/.keep-alive; source edits require a deliberate deployment step.

Start with the listed entry points and `devkit context` if available (or read .toolkit.json).
Use rg for scoped text searches and ast-grep for code structure. Read matching sections,
not whole trees. Run focused tests after edits; use full gates for broad changes.
`devkit run -- <command>` keeps complete local logs and prints a bounded tail, preserving
exit status. Read full failures when needed. Do not hide a failed check or claim token
savings without measurement. Keep generated outputs, private state, credentials and
.toolkit-runs out of commits. Shared tools: https://github.com/louisbrailey/developer-toolkit.
