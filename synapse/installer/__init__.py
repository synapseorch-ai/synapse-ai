"""
The Synapse installer: one screen of named steps, a few questions, then a link.

Standard library only, on purpose. It runs before Synapse is installed — from
`synapse-install.pyz`, a zipapp of this package that `setup.sh` / `setup.ps1`
download and run with a Python uv provides — and also from inside an old v1.x
install's venv, whose packages it cannot count on. The installed CLI imports it
too, for `synapse upgrade`.
"""

#: The Synapse version this installer installs unless told otherwise. Stamped
#: by scripts/build_installer.py when the .pyz is built; "" when running from a
#: checkout, where it is read from pyproject.toml instead.
BUILT_FOR = ""
