"""
Skills (core/skills.py) — instruction packages agents load on demand.

What must be true:

  * the format is Claude Code's SKILL.md, verbatim — frontmatter `name` +
    `description`, markdown body, unknown keys tolerated — so skills written
    for either product work in the other;
  * progressive disclosure holds: the roster (names + descriptions) is in the
    system prompt from turn 0, and a body enters the context only through a
    `load_skill` call;
  * two sources merge into one list — the store-backed library and the
    read-only `.agents/skills/` disk scan — with the library shadowing the
    disk on a name collision;
  * the CRUD routes manage the library only, and refuse in-place renames
    (agents select skills by name).
"""
import json
from pathlib import Path

import pytest

from _fakes import seed as S

from core import skills as skills_mod
from core.skills import (
    MAX_SKILL_BODY_CHARS,
    _PACKAGE_RE,
    SKILL_TOOL_NAME,
    clip_body,
    list_skills,
    normalize_name,
    parse_skill_markdown,
    render_roster,
    resolve_selected,
    save_library,
    scan_disk_skills,
)

def _load_call(skill_name: str) -> str:
    # Not the shared tool_call() helper: its first positional is the tool
    # name, which collides with load_skill's own `name` argument.
    return json.dumps({"tool": SKILL_TOOL_NAME, "arguments": {"name": skill_name}})


GOOD_SKILL = (
    "---\n"
    "name: Market Summary\n"
    "description: Formats the weekly market summary\n"
    "license: MIT\n"
    "---\n"
    "# Steps\nAlways lead with the index moves."
)


# ── parsing ──────────────────────────────────────────────────────────────────

class TestParsing:
    def test_good_skill_parses_and_normalizes(self):
        skill, err = parse_skill_markdown(GOOD_SKILL)
        assert err is None
        assert skill["name"] == "market-summary"        # kebab-cased
        assert skill["description"] == "Formats the weekly market summary"
        assert skill["body"].startswith("# Steps")      # license: ignored, not fatal

    @pytest.mark.parametrize("text,fragment", [
        ("no frontmatter at all", "no YAML frontmatter"),
        ("---\ndescription: x\n---\nbody", "no 'name'"),
        ("---\nname: x\n---\nbody", "no 'description'"),
        ("---\n- a\n- b\n---\nbody", "YAML mapping"),
        ("---\nname: 'bad name!!'\ndescription: d\n---\n", "not a usable skill name"),
    ])
    def test_rejections_explain_themselves(self, text, fragment):
        skill, err = parse_skill_markdown(text)
        assert skill is None
        assert fragment in err

    def test_normalize_name(self):
        assert normalize_name("  My Skill Name ") == "my-skill-name"

    def test_clip_body(self):
        clipped = clip_body("x" * (MAX_SKILL_BODY_CHARS + 100))
        assert len(clipped) < MAX_SKILL_BODY_CHARS + 100
        assert "truncated" in clipped


# ── disk scan + merge ────────────────────────────────────────────────────────

@pytest.fixture
def disk_skills(tmp_path, monkeypatch):
    folder = tmp_path / "market-summary"
    folder.mkdir()
    (folder / "SKILL.md").write_text(GOOD_SKILL, encoding="utf-8")
    (tmp_path / "flat-skill.md").write_text(
        "---\nname: flat-skill\ndescription: A skill in a bare file\n---\nBody.",
        encoding="utf-8",
    )
    (tmp_path / "broken.md").write_text("no frontmatter here", encoding="utf-8")
    monkeypatch.setenv("SYNAPSE_SKILLS_DIR", str(tmp_path))
    return tmp_path


class TestDiskAndMerge:
    def test_scan_finds_both_layouts_and_skips_broken(self, disk_skills):
        names = {s["name"] for s in scan_disk_skills()}
        assert {"market-summary", "flat-skill"} <= names
        assert not any("broken" in n for n in names)

    async def test_library_shadows_disk_on_collision(self, disk_skills):
        await save_library([{
            "id": "market-summary", "name": "market-summary",
            "description": "The library version", "body": "library body",
        }])
        skills = await list_skills()
        library = [s for s in skills if s["name"] == "market-summary" and s["source"] == "library"]
        disk = [s for s in skills if s["name"] == "market-summary" and s["source"] == "disk"]
        assert library and disk
        assert disk[0]["shadowed"] is True

        resolved = await resolve_selected(["market-summary", "no-such-skill"])
        assert [s["source"] for s in resolved] == ["library"]  # unknown skipped, library wins

    def test_home_directory_is_scanned(self, tmp_path, monkeypatch):
        """Skills live in `~/.agents/skills` on a real machine.

        Scanning only the install root and the cwd found nothing on a laptop
        whose skills were all personal — reported as "the skills section is
        empty even though I have skills in .agents/skills".
        """
        home = tmp_path / "home"
        (home / ".agents" / "skills" / "personal-skill").mkdir(parents=True)
        (home / ".agents" / "skills" / "personal-skill" / "SKILL.md").write_text(
            "---\nname: personal-skill\ndescription: Lives in the home directory\n---\nBody.",
            encoding="utf-8",
        )
        monkeypatch.delenv("SYNAPSE_SKILLS_DIR", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert "personal-skill" in {s["name"] for s in scan_disk_skills()}

    def test_roster_carries_names_and_descriptions_only(self):
        roster = render_roster([{"name": "a-skill", "description": "does A"}])
        assert "`a-skill`: does A" in roster
        assert SKILL_TOOL_NAME in roster


# ── engine flow ──────────────────────────────────────────────────────────────

def _server():
    import types
    return types.SimpleNamespace(agent_sessions={}, memory_store=None, tool_router={})


async def _events(fake_llm, script, agent, **kw):
    from core.react_engine import run_agent_step
    fake_llm.script(script)
    return [ev async for ev in run_agent_step(
        message="do the work", agent_id=agent["id"], session_id="s_skill",
        server_module=_server(), agent_override=agent, max_turns=5, **kw,
    )]


@pytest.fixture
async def seeded_skill():
    await save_library([{
        "id": "market-summary", "name": "market-summary",
        "description": "Formats the weekly market summary",
        "body": "SKILL BODY: lead with the index moves.",
    }])


class TestEngineFlow:
    async def test_roster_rides_the_prompt_and_body_loads_on_demand(self, fake_llm, seeded_skill):
        agent = S.make_agent(tools=[], skills=["market-summary"])
        events = await _events(fake_llm, [
            _load_call("market-summary"),
            "done",
        ], agent)

        # Turn 0's system prompt carries the roster but never the body.
        first_call = str(fake_llm.calls[0])
        assert "### SKILLS" in first_call
        assert "Formats the weekly market summary" in first_call
        assert "SKILL BODY" not in first_call

        assert any(e.get("type") == "skill_loaded" and e.get("skill") == "market-summary"
                   for e in events)
        # After the load, the body is in the model's context.
        assert "SKILL BODY: lead with the index moves." in str(fake_llm.calls[1])

    async def test_unknown_and_repeat_loads_are_handled(self, fake_llm, seeded_skill):
        agent = S.make_agent(tools=[], skills=["market-summary"])
        events = await _events(fake_llm, [
            _load_call("nonexistent"),
            _load_call("market-summary"),
            _load_call("market-summary"),
            "done",
        ], agent)
        previews = [e.get("preview", "") for e in events if e.get("type") == "tool_result"]
        assert any("Unknown skill" in p for p in previews)
        assert sum(1 for e in events if e.get("type") == "skill_loaded") == 1
        assert any("already loaded" in p for p in previews)

    async def test_agent_without_skills_gets_no_roster(self, fake_llm, seeded_skill):
        agent = S.make_agent(tools=[])
        await _events(fake_llm, ["just the answer"], agent)
        assert "### SKILLS" not in str(fake_llm.calls[0])


# ── routes ───────────────────────────────────────────────────────────────────

class TestRoutes:
    async def test_crud_roundtrip(self, client):
        created = await client.post("/api/skills", json={
            "name": "Report Style", "description": "House style for reports", "body": "# Style",
        })
        assert created.status_code == 200
        assert created.json()["name"] == "report-style"

        listed = (await client.get("/api/skills")).json()
        assert any(s["name"] == "report-style" and s["source"] == "library" for s in listed)
        assert all("body" not in s for s in listed)  # metas only

        one = (await client.get("/api/skills/report-style")).json()
        assert one["body"] == "# Style"

        updated = await client.put("/api/skills/report-style", json={
            "name": "report-style", "description": "Updated", "body": "# Style v2",
        })
        assert updated.status_code == 200
        assert (await client.get("/api/skills/report-style")).json()["description"] == "Updated"

        assert (await client.delete("/api/skills/report-style")).status_code == 200
        assert (await client.get("/api/skills/report-style")).status_code == 404

    async def test_duplicate_rename_and_missing_are_refused(self, client):
        await client.post("/api/skills", json={"name": "one", "description": "d", "body": ""})
        assert (await client.post("/api/skills", json={
            "name": "one", "description": "again", "body": "",
        })).status_code == 409
        assert (await client.put("/api/skills/one", json={
            "name": "two", "description": "renamed", "body": "",
        })).status_code == 400
        assert (await client.delete("/api/skills/never-existed")).status_code == 404


# ── installing from the Skills CLI ───────────────────────────────────────────

class TestCliInstall:
    """`npx skills add` is a subprocess that writes third-party instructions.

    The tests that matter are the ones about what it refuses to run: the
    package reaches argv, so a name that could become a command is the whole
    risk. Nothing here touches the network — the spawn itself is faked.
    """

    @pytest.mark.parametrize("package", [
        "anthropics/skills",
        "vercel-labs/agent-skills",
        "vercel-labs/agent-skills@react-best-practices",
        "some-skill",
        "owner/repo@v1.2.3",
    ])
    def test_real_packages_are_accepted(self, package):
        assert _PACKAGE_RE.match(package)

    @pytest.mark.parametrize("package", [
        "a; rm -rf /",           # command chaining
        "a && curl evil.sh",
        "a | sh",
        "$(whoami)",
        "`id`",
        "--global",              # a flag, not a package
        "-rf /tmp",
        "a b",                   # a second argv element in disguise
        "owner/repo; touch pwned",
        "",
    ])
    def test_injection_shaped_packages_are_refused(self, package):
        assert not _PACKAGE_RE.match(package)

    async def test_invalid_package_never_spawns_anything(self, monkeypatch):
        spawned = []

        async def explode(*argv, **kwargs):
            spawned.append(argv)
            raise AssertionError("must not spawn for an invalid package")

        monkeypatch.setattr("asyncio.create_subprocess_exec", explode)
        with pytest.raises(skills_mod.SkillInstallError) as exc:
            await skills_mod.install_from_cli("a; rm -rf /")
        assert "not a valid package" in str(exc.value)
        assert spawned == []

    async def test_missing_npx_explains_itself(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _cmd: None)
        with pytest.raises(skills_mod.SkillInstallError) as exc:
            await skills_mod.install_from_cli("anthropics/skills")
        assert "Node" in str(exc.value)

    async def test_success_reports_what_appeared(self, monkeypatch, tmp_path):
        """The package is passed as one argv element, and new skills are named."""
        monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/npx")
        seen: dict = {}

        installed = tmp_path / "installed"
        (installed / "fresh-skill").mkdir(parents=True)
        (installed / "fresh-skill" / "SKILL.md").write_text(
            "---\nname: fresh-skill\ndescription: Arrived from the CLI\n---\nBody.",
            encoding="utf-8",
        )

        class FakeProc:
            returncode = 0

            async def communicate(self):
                # The install "lands" between the two scans.
                monkeypatch.setenv("SYNAPSE_SKILLS_DIR", str(installed))
                return (b"added fresh-skill", b"")

        async def fake_exec(*argv, **kwargs):
            seen["argv"] = argv
            return FakeProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        monkeypatch.setenv("SYNAPSE_SKILLS_DIR", str(tmp_path / "empty"))

        result = await skills_mod.install_from_cli("anthropics/skills")

        assert result["installed"] == ["fresh-skill"]
        assert "anthropics/skills" in seen["argv"]      # one element, not interpolated
        assert "-g" in seen["argv"]                      # user-level install
        assert not any(";" in str(a) for a in seen["argv"])

    async def test_a_failing_cli_surfaces_its_output(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/npx")

        class FailingProc:
            returncode = 1

            async def communicate(self):
                return (b"error: package not found", b"")

        async def fake_exec(*argv, **kwargs):
            return FailingProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        with pytest.raises(skills_mod.SkillInstallError) as exc:
            await skills_mod.install_from_cli("nobody/nothing")
        assert "package not found" in str(exc.value)

    async def test_a_failure_reads_as_the_cli_s_words_not_its_banner(self, monkeypatch):
        """The Skills dialog showed the CLI's block-letter logo and spinner frames (F203).

        Captured from `npx -y skills add vercel-labs/agent-skills@react-best-practices`
        on 2026-09-26: the useful line, "No matching skills found", sat below a
        six-line banner and a run of spinner frames the terminal would have drawn
        over one another (`ESC[1G ESC[J`), where the dialog's box cut it off.
        """
        monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/npx")
        banner = (
            "███████╗██╗  ██╗██╗██╗     ██╗     ███████╗\n"
            "██╔════╝██║ ██╔╝██║██║     ██║     ██╔════╝\n"
            "╚══════╝╚═╝  ╚═╝╚═╝╚══════╝╚══════╝╚══════╝\n"
        )
        spin = "".join(f"{glyph}  Fetching skills…\x1b[1G\x1b[J" for glyph in "◒◐◓◑◒◐")
        raw = (
            f"{banner}\n\x1b[?25l┌   skills \n│\n"
            "◇  Source: https://github.com/vercel-labs/agent-skills.git @react-best-practices\n"
            f"\x1b[?25h\x1b[?25l│\n{spin}◇  Found 1 skill\n\x1b[?25h│\n"
            "■  No matching skills found for: react-best-practices\n│\n●  Available skills:\n│\n"
            "│    - vercel-react-best-practices\n"
        ).encode()

        class FailingProc:
            returncode = 1

            async def communicate(self):
                return (raw, b"")

        async def fake_exec(*argv, **kwargs):
            return FailingProc()

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        with pytest.raises(skills_mod.SkillInstallError) as exc:
            await skills_mod.install_from_cli("vercel-labs/agent-skills@react-best-practices")
        said = str(exc.value)
        assert "No matching skills found for: react-best-practices" in said
        assert "vercel-react-best-practices" in said
        assert "Found 1 skill" in said
        assert "█" not in said and "╚" not in said          # no banner
        assert "Fetching skills" not in said                  # the frames were drawn over
        assert "\x1b" not in said
        assert "\n│\n" not in said                            # no gutter-only lines

    def test_box_rules_go_and_their_words_stay(self):
        said = skills_mod._readable_cli_output("│\n├──────────╯\n■  Failed to install 2\n└  Done!  Review skills before use.\n")
        assert said == "■  Failed to install 2\n└  Done!  Review skills before use."

    def test_windows_line_endings_are_not_redraws(self):
        said = skills_mod._readable_cli_output("■  No matching skills found for: x\r\n│\r\n●  Available skills:\r\n")
        assert said == "■  No matching skills found for: x\n●  Available skills:"


class TestCliRoutes:
    async def test_status_and_refusal(self, client, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/bin/npx")
        assert (await client.get("/api/skills-cli")).json() == {"available": True}

        bad = await client.post("/api/skills/install", json={"package": "a; rm -rf /"})
        assert bad.status_code == 400
        assert "not a valid package" in bad.json()["detail"]
