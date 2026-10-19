# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | ✅ Security fixes during the alpha period |

## Reporting a vulnerability

Please report suspected vulnerabilities privately using GitHub's **Report a
vulnerability** feature in the repository's Security tab
(<https://github.com/metodiev/OpenQVM/security/advisories/new>).

Do not open a public issue or include exploit details in a public discussion.

Include:

- affected OpenQVM version and host platform;
- QEMU version and accelerator;
- a description of the issue and its impact;
- reproduction steps or a proof of concept;
- relevant logs with secrets and personal data removed.

## What to expect

- Acknowledgement within 5 business days.
- An initial assessment and remediation plan within 10 business days.
- Coordinated disclosure after a fix is available, normally within 90 days.
- Credit in the release notes if you want it.

## Scope

In scope:

- command injection or path traversal through OpenQVM input;
- unsafe extraction/import of `.oqv` packages;
- control of QEMU processes or disks belonging to another VM/user;
- unintended privilege escalation;
- exposure of secrets in logs;
- bypass of validation that leads to data corruption.

Out of scope:

- vulnerabilities in QEMU, PySide6/Qt, psutil or the host OS that are reported
  upstream (please report them there as well);
- guest-to-host escapes that are QEMU/hardware issues rather than OpenQVM logic;
- denial of service caused by running untrusted guests on your own machine;
- the documented lack of authentication on loopback QMP/VNC while operating on a
  multi-user host (treat this as a known limitation and avoid that setup);
- social engineering or physical access.

## Security-relevant documentation

See [docs/security.md](docs/security.md) for the threat model, controls and known
limitations.
