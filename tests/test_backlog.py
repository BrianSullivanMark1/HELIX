"""The improvement backlog (services/backlog.py): the user's queued ideas and the night's material,
which the dream session mines first — what the retired Evolve pass used to own."""
from __future__ import annotations

import json

from helix.services.backlog import BACKLOG_FILE, LEGACY_FILE, Backlog


class _Lessons:
    def __init__(self, rules=None):
        self.rules = rules or {"": ["Keep replies short"], "brian": ["Prefer metric units"]}

    def _all(self):
        return dict(self.rules)

    def _rules(self, user=""):
        return list(self.rules.get(user, []))


def test_ideas_queue_dedupe_and_age_out(tmp_path):
    b = Backlog(tmp_path)
    assert b.items() == []
    assert b.add("  Teach the studio   to rotate parts ") is True
    assert b.add("teach the studio to rotate parts") is True  # the same idea, another case — not a duplicate
    assert b.items() == ["Teach the studio to rotate parts"]
    for n in range(30):
        b.add(f"idea {n}")
    items = b.items()
    assert len(items) == 20 and items[-1] == "idea 29" and "Teach the studio to rotate parts" not in items
    data = json.loads((tmp_path / BACKLOG_FILE).read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["items"] == items


def test_a_drafted_idea_is_crossed_off_loosely(tmp_path):
    b = Backlog(tmp_path)
    b.add("Remember the camera device")
    b.add("Shorten the morning brief")
    b.take("  remember   the CAMERA device ")  # the model quotes it back with its own spacing
    assert b.items() == ["Shorten the morning brief"]
    b.take("never queued")
    assert b.items() == ["Shorten the morning brief"]


def test_no_data_dir_disables_the_queue_quietly():
    b = Backlog(None, lessons=_Lessons(), log_tail=lambda: "ERROR x")
    assert b.add("an idea") is False and b.items() == []
    b.take("an idea")
    assert "IMPROVEMENT BACKLOG" in b.material() and "(empty)" in b.material()


def test_the_material_is_the_backlog_then_the_lessons_then_the_log(tmp_path):
    b = Backlog(tmp_path, lessons=_Lessons(), log_tail=lambda: "line1\nERROR reminders: fired twice")
    b.add("Teach the studio to rotate parts")
    text = b.material()
    assert text.index("IMPROVEMENT BACKLOG") < text.index("LESSONS") < text.index("LOG TAIL")
    assert "- Teach the studio to rotate parts" in text
    assert "[default] Keep replies short" in text and "[brian] Prefer metric units" in text
    assert "ERROR reminders: fired twice" in text


def test_a_broken_lessons_store_or_log_is_an_empty_section_not_an_error(tmp_path):
    class _Broken:
        def _all(self):
            raise RuntimeError("store gone")

    def _tail():
        raise OSError("no log")

    b = Backlog(tmp_path, lessons=_Broken(), log_tail=_tail)
    text = b.material()
    assert "(none)" in text and text.rstrip().endswith("(empty)")


def test_evolves_queue_is_folded_in_once_and_the_old_file_removed(tmp_path):
    (tmp_path / LEGACY_FILE).write_text(json.dumps(["old idea one", "Shared idea"]), encoding="utf-8")
    b = Backlog(tmp_path)
    b.add("shared idea")  # the fold runs on the first read, so this dedupes against the folded queue
    assert b.items() == ["old idea one", "Shared idea"]
    assert not (tmp_path / LEGACY_FILE).exists()
    # A second service over the same folder finds nothing left to fold and the queue as it is.
    assert Backlog(tmp_path).items() == ["old idea one", "Shared idea"]


def test_a_damaged_legacy_file_leaves_both_files_alone(tmp_path):
    (tmp_path / LEGACY_FILE).write_text("{not json", encoding="utf-8")
    b = Backlog(tmp_path)
    assert b.items() == []
    assert (tmp_path / LEGACY_FILE).exists()


def test_a_damaged_store_reads_as_empty(tmp_path):
    (tmp_path / BACKLOG_FILE).write_text("[1, 2", encoding="utf-8")
    b = Backlog(tmp_path)
    assert b.items() == []
    assert b.add("fresh") is True and b.items() == ["fresh"]
