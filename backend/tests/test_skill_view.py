"""skill_view: reading a skill's full instructions, and only ever from inside that skill's folder."""

from __future__ import annotations

import pytest

from app.skills_loader import SkillsLibrary
from app.tools.base import ToolContext
from app.tools.productivity import SkillViewTool


@pytest.fixture()
def library(tmp_path):
    skill = tmp_path / "demo-skill"
    skill.mkdir()
    body = "# Demo\n\n" + ("Take the long way round. " * 400)
    (skill / "SKILL.md").write_text(
        f"---\nname: demo-skill\ndescription: A demo skill\n---\n\n{body}", encoding="utf-8"
    )
    (skill / "references").mkdir()
    (skill / "references" / "notes.md").write_text("# Notes\n\nReference body.", encoding="utf-8")
    return SkillsLibrary(directory=tmp_path, scan_mode="off")


@pytest.fixture()
def ctx(library) -> ToolContext:
    return ToolContext(skills=library)


async def test_returns_the_whole_body_not_the_first_screen(library, ctx) -> None:
    """skill_run truncates at 4000 chars; a view exists precisely so long skills stay readable."""
    result = await SkillViewTool().run({"name": "demo-skill"}, ctx)
    assert result.ok
    assert len(result.output["body"]) > 4000
    assert result.output["truncated"] is False


async def test_reads_a_reference_file_from_inside_the_skill(ctx) -> None:
    result = await SkillViewTool().run({"name": "demo-skill", "file": "references/notes.md"}, ctx)
    assert result.ok
    assert result.output["file"] == "references/notes.md"
    assert "Reference body." in result.output["content"]


async def test_cannot_reach_outside_the_skill_folder(ctx) -> None:
    for escape in ("../../outside.md", "../demo-skill/../../../etc/passwd", "/etc/passwd"):
        result = await SkillViewTool().run({"name": "demo-skill", "file": escape}, ctx)
        assert not result.ok, escape


async def test_missing_file_is_reported_not_invented(ctx) -> None:
    result = await SkillViewTool().run({"name": "demo-skill", "file": "references/nope.md"}, ctx)
    assert not result.ok
    assert "no such file" in result.error


async def test_unknown_skill_lists_what_is_available(ctx) -> None:
    result = await SkillViewTool().run({"name": "not-a-skill"}, ctx)
    assert not result.ok
    assert "demo-skill" in result.error
