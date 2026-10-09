# Hermes Access Skill

Use this workspace only from approved devices and approved identities.

Operational rules:

- Treat Cloudflare Access, FRP/Nginx, or Tailscale as the outer network boundary.
- Treat Hermes device approval as the inner application boundary.
- Keep coding agents in separate workspaces or containers.
- Do not expose raw terminals directly to the public internet without an identity gateway and audit logging.
- Put reusable local instructions in this directory as Markdown files.
