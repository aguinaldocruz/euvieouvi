# Security policy

[Português do Brasil](SECURITY.pt-BR.md) · English

## Supported versions

euvieouvi is currently a pre-release project. Security fixes are applied to the latest code on the
default branch. Older commits, forks, and unmaintained images are not supported.

## Reporting a vulnerability

Do not open a public issue, discussion, or pull request for a suspected vulnerability. Use GitHub's
**Report a vulnerability** private security-advisory flow for this repository. Include affected
version/commit, deployment model, reproduction steps, impact, and any proposed mitigation. Remove
real credentials, webhook tokens, database content, and personal media history.

If private reporting is unavailable, contact the repository owner privately through a channel they
publish on their GitHub profile and ask for a secure reporting method without disclosing details.

Maintainers should acknowledge a complete report when available, validate severity, coordinate a
fix and disclosure, and credit the reporter if requested and appropriate. Response times are not
guaranteed because this is a volunteer project.

## Deployment boundary

The application has no built-in authentication and must not be exposed directly to an untrusted
network. Use an authenticated reverse proxy, TLS, network restrictions, and a long random
`EUVIEOUVI_SECRET_KEY`. Protect the instance directory and backups; they contain connector
credentials, webhook tokens, settings, and playback history.
