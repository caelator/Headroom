# Security policy

## Report a vulnerability privately

Use **Security → Report a vulnerability** on the [Headroom repository](https://github.com/caelator/Headroom/security/advisories/new). GitHub private vulnerability reporting is enabled. Do not post vulnerabilities involving credential exposure or unauthorized access in public issues.

Describe the affected version or commit, the attack conditions, the expected boundary, and a minimal reproduction using synthetic data. Fixes are focused on the current default branch; there is no separate long-term support branch or promised response time.

## Information to omit

Never include passwords, OAuth tokens, API keys, cookies, raw account responses, conversation text, private databases, raw launchd environments, or credential metadata. Redact personal account identifiers and screenshots before sharing. Do not attach a complete provider response or database to a report.

For connection problems, start with `python3 scripts/diagnostics.py`. Review its allowlisted output before sharing and include only the status or error codes needed to reproduce the issue. Ordinary bugs belong in the [bug form](https://github.com/caelator/Headroom/issues/new?template=bug_report.yml).

## Security boundaries

The service binds to IPv4 loopback, validates Host and Origin, and requires a local action header for writes. Provider collectors read existing credentials, use fixed endpoints, refuse credential-bearing redirects, and persist sanitized quota data. Account continuity uses a private HMAC salt. Headroom is intended to run on your own machine; exposing its port through a public proxy changes that boundary.

See [operations and recovery](docs/operations.md) for private storage and backup requirements and [measurement contracts](docs/metrics.md) for identity, freshness, and reset rules.
