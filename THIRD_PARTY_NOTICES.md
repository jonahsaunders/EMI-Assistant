# Third-party licenses and source information

EMI Assistant's original source code is licensed under the MIT license in
`LICENSE`. Third-party software and copied upstream files retain their own
copyrights and licenses. The MIT label does not relicense those components.

**EMI Assistant uses PySide6, Shiboken6, and the Qt Core, GUI, and Widgets
libraries under their LGPL version 3 option.** Qt is copyright The Qt Company
Ltd. and its contributors. The full LGPLv3 and the incorporated GPLv3 text are
included in `LICENSES/`. The application permits modification and replacement
of compatible libraries, including reverse engineering to debug modifications.

The app's **Licenses** button displays these notices and the bundled license
texts without requiring a network connection.

## Files actually copied into this repository and installer

The three unmodified JSON schemas in `validation/schemas/` are from KiCad and
are distributed under **GPL-3.0-or-later**, separately from the original MIT
application. Their source form is included. The schema README records immutable
upstream revisions and SHA256 hashes. KiCad's license text and licensing-scope
statement are preserved in `LICENSES/GPL-3.0.txt` and
`LICENSES/KiCad-LICENSE-README.txt`.

These validation files are kept as separate upstream data. Their inclusion
does not purport to change either KiCad's license or the original application's
MIT grant. KiCad's upstream licensing statement is available at
https://github.com/KiCad/kicad-source-mirror/blob/master/LICENSE.README .

## Dependencies obtained separately at runtime

The source repository and KiCad installer do **not** contain Qt/PySide wheels,
KiCad binaries, ngspice binaries, openEMS binaries, or a populated Python
environment. The launcher installs libraries from PyPI into the user's plugin
environment. Solver setup downloads official upstream archives directly to
the user's computer and preserves their contents.

| Component | License information | Official source / notices |
|---|---|---|
| kicad-python 0.8.0 | MIT; copyright The KiCad Developers | https://gitlab.com/kicad/code/kicad-python/-/blob/0.8.0/LICENSE |
| PySide6-Essentials and Shiboken6 | LGPL-3.0-only option used here; alternative upstream GPL/commercial options also exist | https://doc.qt.io/qtforpython-6/licenses.html |
| Qt Core, GUI and Widgets | LGPLv3 option; bundled third-party components retain additional terms | https://doc.qt.io/qt-6/lgpl.html |
| NumPy | Core BSD-3-Clause; wheels include additional licensed components | https://numpy.org/doc/stable/license.html |
| Shapely | BSD-3-Clause; its GEOS library uses LGPLv2.1 | https://shapely.readthedocs.io/ and https://libgeos.org/ |
| py7zr | LGPL-2.1-or-later; compression dependencies retain their own terms | https://github.com/miurahr/py7zr/blob/master/LICENSE |
| ngspice 47 | Mixed license distribution: modified BSD base plus components under LGPL, MPL, GPL, and other terms; consult the release COPYING | https://sourceforge.net/projects/ngspice/files/ng-spice-rework/47/ |
| openEMS 0.0.36 | GPL-3.0-or-later; its dependencies have their own licenses | https://github.com/thliebig/openEMS/blob/v0.0.36/COPYING |

The table identifies direct dependencies and important underlying libraries;
it is not an exhaustive binary bill of materials. Transitive Python packages,
Qt plugins, numerical libraries, and archive codecs retain the licenses shipped
with their exact installed versions. Do not remove upstream notices or apply
the app's MIT label to an entire downloaded distribution.

## Sources and compatible replacement libraries

For PySide/Shiboken, obtain source matching the installed version from
https://download.qt.io/official_releases/QtForPython/pyside6/ . Matching Qt
sources are at https://download.qt.io/official_releases/qt/ . Qt documents
component notices at https://doc.qt.io/qtforpython-6/licenses.html and its LGPL
requirements at https://www.qt.io/development/open-source-lgpl-obligations .

On Windows the default private environment is
`%LOCALAPPDATA%\KiCad\10.0\python-environments\org.emi-assistant.kicad`.
Its `Scripts/python.exe` can inspect package versions using `-m pip show`
and install a locally built, interface-compatible replacement using `-m pip
install`. Close EMI Assistant before replacing libraries. Other platforms use
the Python environment selected or created by KiCad. Normal Python imports
load these libraries at runtime; the app is not frozen or statically linked.
Bootstrap checks whether imports work and does not reinstall a working
replacement merely because its version or hash differs.

ngspice 47 source and the actual release notices are available in the same
official release directory as its binary archive:
https://sourceforge.net/projects/ngspice/files/ng-spice-rework/47/ . The archive
contains `Spice64/docs/COPYING`. That file, rather than a blanket BSD label,
describes the mixed distribution.

The openEMS source release is
https://github.com/thliebig/openEMS/tree/v0.0.36 and its coordinating project
release is https://github.com/thliebig/openEMS-Project/tree/v0.0.36 . Preserve
the project's submodule/dependency revisions when rebuilding. This app sends
generated XML to the standalone solver and reads its output files.

Users may select compatible installed solvers through `EMI_NGSPICE` and
`EMI_OPENEMS`. Hash checks protect automatic downloads; they do not prohibit
using an explicitly selected, working modified solver.

## Redistributing binaries or changing the package

This package provides the original app's editable Python source. If you later
bundle, mirror, freeze, modify, or redistribute dependency binaries, review the
exact builds and their licenses again. Retain all notices and satisfy applicable
GPL/LGPL corresponding-source, relinking, and installation-information duties.
A link to a project's latest source branch is not a substitute for the complete
matching source and build materials when those are required.

These notices document the inspected distribution. They are not a legal
guarantee about future contributions, unknown third-party code, patents, or a
different packaging method. The individual license texts govern.
