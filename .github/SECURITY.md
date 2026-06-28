# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report them privately using GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability):
go to the repository's **Security** tab → **Report a vulnerability**. This opens
a private channel with the maintainers.

Please include, as far as you can:

- A description of the issue and its impact.
- Steps to reproduce (a minimal proof of concept helps).
- Affected version or commit.

We aim to acknowledge reports within a few days and will keep you updated as we
work on a fix. Please give us reasonable time to address the issue before any
public disclosure.

## Scope notes

Strata runs **locally** and talks to the Anthropic API. A few things to keep in
mind when assessing a report:

- Secrets (e.g. `ANTHROPIC_API_KEY`) are read from environment variables or a
  local `.env` file and must never be committed. `.env` and `*.db`/`*.sqlite*`
  files are git-ignored by design.
- The FastAPI surface and Console are intended for **local** use; exposing them
  on an untrusted network is out of the supported configuration.

Thanks for helping keep Strata and its users safe.
