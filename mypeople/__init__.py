"""mypeople — self-hostable runtime that orchestrates Claude Code, Codex and Grok agents as a team.

Fase 1: packages the already-verified mypeople runtime (board + HUD + terminal + Boss)
as an installable Python package. First-run configures + starts the daemons; it never
generates code (the seed-hydration step is retired). The recipient brings their own
agent credential — this package carries none.
"""
__version__ = "5.15.0"
