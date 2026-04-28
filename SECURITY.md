# Security Policy

## Supported Versions

We actively support the following versions of hb-market-connector with security updates:

| Version | Supported          |
| ------- | ------------------ |
| 0.x     | :white_check_mark: |

The latest minor release receives security fixes. Older releases are not actively
maintained.

## Reporting a Vulnerability

We take the security of hb-market-connector seriously. If you believe you have
found a security vulnerability, please report it privately.

**Please do NOT report security vulnerabilities through public GitHub issues.**

### Preferred Method: GitHub Security Advisories

1. Go to our [Security Advisories page](https://github.com/MementoRC/hb-market-connector/security/advisories)
2. Click "Report a vulnerability"
3. Fill out the form with details about the vulnerability

### Alternative: Email

Send an email to `claude.rc@gmail.com` with the subject line
"Security Vulnerability Report — hb-market-connector".

### What to Include

- **Type of issue** (e.g., authentication bypass, data exposure, injection)
- **Affected component** (e.g., `auth/`, `transport/`, `exchanges/coinbase/`)
- **Location of the affected code** (tag/branch/commit or direct URL)
- **Reproduction steps** with proof-of-concept if possible
- **Impact assessment**, including how an attacker might exploit the issue

### Response Timeline

| Stage | Target |
|-------|--------|
| Acknowledgement | 48 hours |
| Initial assessment | 7 business days |
| Status update | Every 14 days thereafter |
| Coordinated disclosure | Agreed case-by-case |

## Scope

**In scope:**
- Code in this repository: framework abstractions (`auth/`, `rate_limits/`,
  `symbols/`, `ws_models/`, `transport/`) and exchange adapters (`exchanges/`)
- Authentication signing logic (`SigningSpec`, HMAC, JWT)
- WebSocket auth models and message handling

**Out of scope:**
- Vulnerabilities in third-party dependencies — please report upstream
- Issues in dependent projects (e.g., Hummingbot `bleeding-edge`) — report there
- Findings that require physical access or a compromised developer environment

## Security Features

hb-market-connector includes built-in security practices:

- **Declarative auth** (`SigningSpec`) — signing logic is explicit and auditable
- **CodeQL analysis** — automated vulnerability detection on every push and PR
- **Dependency scanning** — weekly Safety and Bandit scans via GitHub Actions
- **Secret detection** — CI pipeline blocks committed credentials
- **Dependabot** — weekly dependency updates with auto-merge for patch bumps

## Security Best Practices for Users

- Never hardcode API keys or secrets in source code; use environment variables
- Rotate API keys immediately if they are exposed
- Use HTTPS/WSS connections and validate SSL certificates
- Review Dependabot alerts and keep dependencies updated

---

Last updated: 2026-04-27
