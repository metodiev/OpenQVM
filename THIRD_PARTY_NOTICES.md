# Third-party notices

OpenQVM is licensed under the Apache License 2.0. That license does **not**
override the licenses of third-party components. This file is informational, not
legal advice; audit dependencies before distributing binaries.

## Included as Python dependencies

| Component | License | Use |
| --- | --- | --- |
| [PySide6](https://pypi.org/project/PySide6/) / Qt for Python / Qt 6 | LGPLv3 (also GPLv2/GPLv3/commercial alternatives) | Desktop GUI, networking primitives, image handling |
| [psutil](https://pypi.org/project/psutil/) | BSD-3-Clause | Host and process metrics |
| Python standard library | PSF License | Runtime and tooling |

PySide6 is used as a dynamically linked library. If you distribute OpenQVM
binaries, review the LGPLv3 relinking requirements and Qt's third-party notices,
and include the applicable license texts. Qt itself contains additional
third-party components listed in the Qt distribution.

## External programs (not bundled by this repository)

| Component | License | Use |
| --- | --- | --- |
| [QEMU](https://www.qemu.org/) | GPLv2 (with some LGPL/BSD components) | Virtualization backend, invoked as a separate executable |
| [edk2/OVMF](https://github.com/tianocore/edk2) | BSD-2-Clause (mostly) | UEFI firmware, installed with QEMU or the OS |
| Alpine Linux installation media | Various free-software licenses | Optional test ISO, not included |
| Optional: swtpm | BSD-3-Clause / LGPL | TPM emulation (detected, not implemented yet) |

OpenQVM invokes QEMU as a separate process and does not link against QEMU
libraries. If you redistribute QEMU together with OpenQVM, you must comply with
QEMU's GPLv2 terms, including source availability. Do not redistribute proprietary
operating-system installation media.

## Development-only dependencies

| Component | License |
| --- | --- |
| pytest | MIT |
| pytest-timeout | MIT |
| ruff | MIT |
| setuptools, wheel, build | MIT |
| PyInstaller (optional) | GPLv2 with a special exception for bundled applications |

Development-only tools are not part of the runtime distribution.

## Assets

The application icon is drawn at runtime by `src/openqvm/ui/icons.py`; no
third-party icon assets are bundled. Screenshots in `docs/images/` were generated
from this application with a locally installed Alpine Linux test guest.
