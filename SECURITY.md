# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/augbastos/Deputy-MCP/security/advisories/new).
Do not open a public issue. Include the version or commit, what an attacker can do, and
the steps to reproduce. Never include a real Deputy token, OAuth secret, calendar-feed
link or anyone's roster data; fictional values are enough.

You can expect an acknowledgement within a week. Fixes land on `main` and are noted in
the [CHANGELOG](CHANGELOG.md).

## Supported versions

Only the latest release on `main` receives security fixes.

## Scope

In scope: anything that lets Deputy MCP disclose a credential or personal data, send a
credential to a host other than the user's Deputy install, perform a write without the
operator's opt-in, or bypass the confirmation required for open-shift claims.

Out of scope: vulnerabilities in Deputy itself (report those to Deputy), in an MCP
client, or an attack that requires control of the user's machine or operating-system
account, which can read the keychain anyway.

## Design notes for reviewers

- Tokens are sent only to `*.deputy.com` unless `DEPUTY_ALLOW_CUSTOM_HOST` is set, and the
  same allowlist applies to hosts named by the OAuth token response and a stored token.
- OAuth tokens are stored in the OS keychain through `keyring`; the plaintext file store
  is used only when `DEPUTY_TOKEN_STORE` is set.
- A `.env` found in the working directory cannot enable writes, allow custom hosts or
  move the token store.
- Error text shown to a model or printed by the CLI goes through
  `deputy_mcp.sanitize`.
