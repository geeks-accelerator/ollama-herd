# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| 0.1.x   | Yes                |

## Reporting a Vulnerability

If you discover a security vulnerability in Ollama Herd, please report it responsibly.

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, please email **security@geeks-accelerator.com** with:

- A description of the vulnerability
- Steps to reproduce the issue
- The potential impact
- Any suggested fixes (optional but appreciated)

## What to Expect

- **Acknowledgment** within 48 hours of your report
- **Status update** within 7 days with an assessment and timeline
- **Credit** in the fix commit and changelog (unless you prefer to remain anonymous)

## Scope

This policy covers the Ollama Herd codebase, including:

- The router server (`herd`)
- The node agent (`herd-node`)
- All API endpoints and request handling
- Configuration parsing and environment variable handling

## Design Considerations

Ollama Herd is designed for **trusted LAN environments**. It does not include authentication or encryption by default. If you're exposing the router to untrusted networks, you should place it behind a reverse proxy with TLS and access controls.

Thank you for helping keep Ollama Herd safe.
