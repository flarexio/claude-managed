# Security Policy

## Reporting a vulnerability

Please report security issues through GitHub's private vulnerability reporting for this repository. Do not open a public issue containing secrets, webhook payloads, transcript contents, or exploitable details.

## Security boundaries

`claude-managed` is a sender and launcher, not an authorization system. It validates local paths and authenticates webhook requests, but the receiver must independently enforce:

- HMAC verification with constant-time comparison;
- timestamp freshness and request-ID deduplication;
- project and transcript path allowlists;
- safe action and permission policy;
- verification of repository state and test evidence;
- human approval for irreversible or privileged operations.

A Claude lifecycle event is untrusted input and never proves task completion.

## Secret handling

- Keep the HMAC secret outside Git and Claude settings.
- Secret files must be private regular files, not symlinks, and must not grant group or other access; runtime forwarding and `claude-managed doctor` both enforce this.
- Use a different secret for each trust boundary or receiver.
- Rotate the sender and receiver together if a secret may have been exposed.
- Prefer loopback HTTP. Remote webhook endpoints must use HTTPS.

## Supported versions

Until a stable release exists, only the latest published version receives security fixes.
