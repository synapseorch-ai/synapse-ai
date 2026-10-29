"""The browser kit says the product's name its host gives it, never one of its own (BRW2, D330; Naveen 10-29).

Cloud copies `components/browser` into its web (pro `scripts/sync-ui.sh`), where the product is SynapseOrch, not
Synapse: a sentence the kit wrote with "Synapse" in it told a cloud customer who keeps their site's session under the
wrong name. So the kit reads the name from its host (`BrowserHost`, `SessionsHost`, `RecordingsHost`, each with a
`product`), and each host says its product's: OSS's "Synapse" here, cloud's "SynapseOrch" in its own tests.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "frontend" / "src"
KIT = SRC / "components" / "browser"

#: OSS's hosts for the kit, and the type each one is.
HOSTS = {"lib/browserHost.ts": "BrowserHost", "lib/sessionsHost.ts": "SessionsHost",
         "lib/recordingsHost.ts": "RecordingsHost"}

COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*|\{/\*.*?\*/\}", re.S)


def test_the_kit_names_no_product_itself():
    named = sorted(
        f"{p.relative_to(SRC)}:{n}"
        for p in KIT.rglob("*.ts*")
        if ".test." not in p.name
        for n, line in enumerate(COMMENT.sub(lambda m: "\n" * m.group().count("\n"), p.read_text("utf-8"))
                                 .splitlines(), 1)
        if re.search(r"\bSynapse", line)
    )
    assert not named, f"the kit says a product's name of its own (use its host's `product`): {named}"


def test_every_host_type_carries_the_product():
    for name in set(HOSTS.values()):
        found = [p for p in KIT.glob("*.ts") if re.search(rf"export type {name} = \{{", p.read_text("utf-8"))]
        assert len(found) == 1, f"{name} is declared once in the kit"
        body = found[0].read_text("utf-8").split(f"export type {name} = {{", 1)[1].split("\n};", 1)[0]
        assert re.search(r"^\s*product: string;", body, re.M), f"{name} has a required `product: string`"


def test_oss_hosts_say_synapse():
    for path, kind in HOSTS.items():
        text = (SRC / path).read_text("utf-8")
        assert re.search(rf": {kind}\b", text), f"{path} is a {kind}"
        assert re.search(r"^\s*product: 'Synapse',$", text, re.M), f"{path} says its product is Synapse"
