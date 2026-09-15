<!--
  An AI-use disclosure is required: the `scpe` check fails without one
  (.github/workflows/scpe.yml, https://github.com/augbastos/scpe). Tick exactly one box
  below, or add an Assisted-by trailer to a commit message instead.

  Note for whoever edits this template: never write that trailer at the start of a line
  in this file, comments included. The check scans the raw PR body, so an example
  trailer here would satisfy it for every PR that leaves the template untouched.
-->

## What this changes

<!-- One or two sentences. What is different after this merge, and why. -->

## AI use

<!-- Tick ONE. Both answers are welcome; only silence fails the check. -->

- [ ] I used generative AI for part of this change
- [ ] I did not use generative AI for this change

<!-- If you used AI, naming the tool helps a reviewer calibrate. -->

## Checks

- [ ] `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy` pass locally, or this change carries no code
- [ ] Tests and fixtures use fictional data only; no real credentials, employees or installs
- [ ] I read [CONTRIBUTING.md](../CONTRIBUTING.md)
