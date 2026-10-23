# Security

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/mgchi/agent-setup/security/advisories/new)
for disclosure bypasses, unexpected data access, credential exposure, or other
security issues. Include the affected version, a minimal reproduction with
synthetic data, the expected boundary, and the observed behavior.

Please keep vulnerability details out of public issues while a fix is being
coordinated. Do not attach real private context, credentials, transcripts, local
databases, or account configuration. If a credential was exposed, revoke it with
the provider; removing a Git commit cannot invalidate a credential or erase
copies other people already have.

This project is maintained on a best-effort basis and has no guaranteed response
time. Security fixes target the latest released version.

## Trust and data boundaries

- Context is stored on your computer. A hosted lead model receives the context
  loaded into its session.
- Advisers receive sanitized task, candidate, and evidence text. Their worker
  sessions are temporary; provider-side retention follows provider policy.
- Disclosure checks are defense in depth. They are not a sandbox or a guarantee
  against an agent copying sensitive content into an allowed action.
- The installer edits local agent configuration, instruction sections, and hooks.
  Review it before running it. Use `--home` for an isolated evaluation.
- External-source refresh is opt-in and depends on separately authenticated
  connectors. Narrow its retrieval scope before enabling it.
- Anyone with access to your local files may be able to read stored context.
  Protect your account, file permissions, backups, and disk encryption.

Keep secrets out of memory and review material before sharing it. Use encrypted
transfers for private-state migration. Repository ignore rules help prevent
accidental commits, but cannot stop a forced add or content pasted into a file.
