# Contributing

EMI Assistant aims to make useful EMI investigations accessible from one
**Analyze EMI** button. Changes should keep that default workflow simple and
show the evidence and assumptions behind each result.

## Set up a development environment

Use Python **3.10 or later**. From the repository root:

```sh
python -m venv .venv
```

Activate the environment with `source .venv/bin/activate` on Linux/macOS or
`.venv\Scripts\Activate.ps1` in Windows PowerShell. Then install the project:

```sh
python -m pip install -e ".[dev]"
python -m emi_assistant --demo
```

The native interface uses Qt, so Linux may also need the Qt platform libraries
provided by its distribution. File and example modes work without a running
KiCad session. Live integration needs KiCad 10 with its API enabled.

`requirements.txt` is deliberately comment-only: KiCad must register the launcher
before downloading application libraries. The visible first-launch setup uses
`runtime-requirements.txt`. Keep its bounds aligned with `pyproject.toml` when
changing runtime dependencies.

## Run checks

```sh
python -m pytest -q
python -m emi_assistant --demo --analyze --layout-only --output review.html
python tools/build_package.py
```

Qt tests select the `offscreen` platform automatically. If your environment
overrides that setting, set `QT_QPA_PLATFORM=offscreen` before running tests.
The package builder requires `jsonschema`, included in the development extras,
and validates metadata against the bundled KiCad schemas. It writes the
installable archive to `dist/`.

Tests that execute real solvers skip when the corresponding runtime is absent.
To exercise them, provide installed executables with `EMI_TEST_NGSPICE` and
`EMI_TEST_OPENEMS`; `EMI_TEST_NGSPICE_SHARED` can select an ngspice shared library.
For example, after setting those environment variables:

```sh
python -m pytest -q tests/test_sim_ngspice.py tests/test_sim_openems.py
```

A passing mocked-process test is not evidence that a numerical solver or native
KiCad interaction works. Record real solver versions and the commands used when
changing those integrations. Follow the relevant procedures in
[docs/VALIDATION.md](docs/VALIDATION.md) for native acceptance testing.

## Propose a change

Open an issue for a bug or a proposed feature. Include:

- KiCad, EMI Assistant, Python, and operating-system versions.
- What you did, what happened, and what you expected.
- Relevant error text or logs, and a minimal board or schematic you can share.
- For a simulation issue, the model assumptions and solver version.

For a pull request, create a branch and explain the problem, the resulting
behavior, and how you verified it. Add a focused regression test for a bug when
it protects meaningful behavior. Update the user guide or model documentation
when the workflow or supported scope changes.

Useful areas include native KiCad installation testing, real-world geometry
fixtures, additional supported passive topologies, and measured A/B validation.
Numerical results must keep units, input identity, assumptions, and evidence.
Missing information must remain distinguishable from a completed check.

## Share only intended files

Do not commit private designs, credentials, local virtual environments, solver
installations, or application caches. Check generated logs and screenshots for
project names and local paths. Use small, purpose-built fixtures for tests.
The project's source is [MIT licensed](LICENSE); include attribution and
compatible licensing for any contributed third-party material.
