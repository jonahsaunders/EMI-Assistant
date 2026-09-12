# Installer fixes in 0.1.1 and 0.2.0

## Missing toolbar button and dependencies: fixed setup flow in 0.2.0

A reported Windows installation had a valid package and private Python
environment, but `kipy`, `PySide6.QtWidgets` and `shapely` were missing. KiCad did
not show the action because dependency preparation had not completed. A manual
repair made the button appear; that repair is no longer the intended setup flow.

Version 0.2.0 uses a standard-library-only `launch.py` and comment-only
`requirements.txt`. KiCad can register Analyze EMI without downloading the UI
libraries. On the first explicit click, the launcher checks required imports,
including native-library load errors. Missing libraries are installed from
`runtime-requirements.txt` using KiCad's existing private virtual environment.
The bootstrap refuses global Python installation and verifies imports after pip
finishes. NumPy and py7zr are included for solver processing and archive setup.

Windows shows a preparation console, then opens the assistant automatically.
If setup fails, it shows a native error and opens `EMI-first-launch.log` from the
plugin's environment. The normal Windows path is:

```text
%LOCALAPPDATA%\KiCad\10.0\python-environments\org.emi-assistant.kicad\EMI-first-launch.log
```

Keep Analyze EMI available and click again after resolving the reported error.
There is no repair command to paste or separate installer to launch. An already
complete environment does not contact the network on application startup.
Simulation engines have their own private cache and visible progress in the
Simulations tab; engine setup failure leaves layout analysis available.

If the action is missing entirely, verify the package under Plugin and Content
Manager's Installed tab, enable KiCad API in Preferences, and check PCB Editor's
Plugins list. If KiCad reports a damaged Python environment, use its **Recreate
Plugin Environment** action and retry. The bootstrap does not change KiCad
settings, other plugins, system PATH or system Python.

The new native Windows startup flow has automated subprocess tests but has not
been exercised in a Windows/KiCad GUI in the build environment.

## Package schema validation: fixed in 0.1.1 and retained

The original 0.1.0 ZIP omitted `author.contact` and top-level `resources` from
`metadata.json`. KiCad's actual PCM schemas require these fields. They are now
present as empty objects; fabricated contact details or links are unnecessary.

The build validates both the source metadata and the finished ZIP against
upstream PCM v1/v2 schemas and the IPC plugin schema. Regression tests prove
that the old missing-field combination and each omission separately are rejected.
The ZIP also includes the metadata needed to rebuild it from the included source.

This fixes confirmed schema-validation failures. The app still requires KiCad
10.0.x. A native KiCad installation has not been run in this build environment.
If installation still fails, report the full error text and KiCad version from
Help > About KiCad.
