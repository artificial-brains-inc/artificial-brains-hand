# Security

The controls run on your computer's loopback interface with a session-specific
address. Keep that address local; the UI is designed for direct use in your
browser rather than public Internet access.

Store the device API key in `.env`. Git ignores that file, local configuration,
captured baselines, thumb settings, and run logs. Logs can contain device
identifiers, local paths, and experiment data, so review them before sharing.

For credential exposure or a vulnerability, use your private Artificial Brains
support channel rather than posting secrets in a public issue. Revoke an exposed
API key through the device's supported controls. Ordinary bug reports can include
redacted log excerpts and steps to reproduce the issue.
