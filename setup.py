"""
Synapse 1.x was installed by this file; 2.0 installs with synapse/installer/.

Kept because an older setup.sh / setup.ps1 — the ones the README served from
the `main` branch — download the newest release's source and run `python
setup.py` from it. That still works: this hands over to the installer, which
installs Synapse properly, moves any 1.x data, and removes the unpacked source
tree it was carried in.

    python setup.py            # install (or upgrade) interactively
    python setup.py --yes      # ask nothing
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if sys.version_info < (3, 11):
    sys.exit("Synapse needs Python 3.11 or newer. Install it with:\n"
             "  curl -LsSf https://github.com/synapseorch-ai/synapse-ai/releases/latest/download/setup.sh | bash")

sys.path.insert(0, str(ROOT))
from synapse.installer.__main__ import main  # noqa: E402

args = [a for a in sys.argv[1:] if a != "--upgrade"]
if "--upgrade" in sys.argv[1:]:
    args = ["upgrade", *args]
sys.exit(main(["--carrier", str(ROOT), *args] if not (ROOT / ".git").exists() else args))
