# Security policy

## Reporting a vulnerability

Do not post live credentials, private player data, production databases, server IP details that are not already public, or a working denial-of-service procedure in a public issue.

Use GitHub private vulnerability reporting when it is enabled for the repository. If it is unavailable, open a minimal public issue requesting a private contact channel without including exploit details.

Include:

- affected version and game;
- whether the issue is server-side, client-side, website-side, or deployment-specific;
- a minimal sanitized reproduction;
- expected impact;
- relevant logs with identities, IP addresses, tokens, and paths removed.

## Scope

Security reports are useful for authentication, authorization, account isolation, parser robustness, malformed network input, denial of service, unsafe file handling, secret exposure, website session/CSRF handling, and release-package privacy failures.

Cheating reports, ordinary gameplay bugs, and requests to attack third-party servers are not security vulnerabilities in this project.

## Supported versions

Until stable public releases and a version-support policy exist, only the latest tagged release is intended to receive security fixes.
