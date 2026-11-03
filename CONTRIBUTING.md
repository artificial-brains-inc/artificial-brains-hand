# Contributing

We welcome new experiments, clearer explanations, sensor integrations, UI
improvements, and bug fixes. Share what you observe and help make this example
a useful starting point for other developers.

## Report an observation or issue

Describe your hand and sensor setup, the requested pose, what happened, and what
you expected. Include the relevant SDK versions and configuration changes. A
short log excerpt or screen recording can help connect the observation to the
neural activity and applied movement. Remove API keys, device identifiers, and
private paths before sharing.

## Work on a change

Fork the repository and create a branch. The [neural-grip guide](docs/neural-grip.md)
explains the circuit; [AGENTS.md](AGENTS.md) describes the project conventions.
Keep neural decisions, sensor conditioning, and motor-command translation
separate so other developers can follow the behavior.

Run the local checks:

```bash
python -m pip install -r requirements-test.txt
python -m pytest -q
python run.py --check-offline
python tools/check_project.py
```

With the runtime SDKs installed, also run `python run.py --check`. For neural
changes, regenerate the design, inspect it on the Canvas, and rebuild before a
hardware trial:

```bash
python make_brain.py
ab design --brain brain.abdesign.json
ab build brain --brain brain.abdesign.json --env .env
```

Wait for READY before running. For UI changes, follow the browser tests in
[Testing](docs/testing.md).

In a pull request, explain the relationship or behavior you changed and the
checks you ran. Distinguish software fixtures from physical observations.
Keep `.env`, captured baselines, saved thumb positions, and run logs local.

## License

Original contributions are submitted under the [project license](LICENSE),
unless separately agreed. You retain ownership of your original work. Preserve
attribution and licenses for any third-party material you include.
