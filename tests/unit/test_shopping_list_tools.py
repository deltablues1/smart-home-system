"""The shopping list is spoken to, so the matching has to be forgiving --
and the completing has to not be.

Marking the wrong item bought is not a cosmetic error: the user comes home
without it and only finds out at dinner. So these tests spend most of their
weight on what the tools must REFUSE to do -- guess between two candidates,
complete a list when one named exception could not be resolved, or report a
change that did not happen.

Run with:
    pytest tests/unit/test_shopping_list_tools.py -v
"""

import pytest

from tools.adk_tools import ha_shopping_tools as sl

ENTITY = "todo.shopping_list"


class FakeHA:
    """A stand-in HA holding the list, so the tools drive real state."""

    def __init__(self, items=()):
        self.items = [
            {"summary": s, "uid": f"u{i}", "status": sl.OPEN}
            for i, s in enumerate(items)
        ]
        self.calls = []

    def __call__(self, path, payload=None, timeout=10.0):
        self.calls.append((path, payload))
        if "get_items" in path:
            wanted = (payload or {}).get("status")
            items = [i for i in self.items if not wanted or i["status"] == wanted]
            return {"service_response": {ENTITY: {"items": items}}}
        if path.endswith("add_item"):
            self.items.append(
                {"summary": payload["item"], "uid": "new", "status": sl.OPEN}
            )
            return {}
        if path.endswith("update_item"):
            for i in self.items:
                if i["summary"] == payload["item"]:
                    i["status"] = payload["status"]
            return {}
        if path.endswith("remove_item"):
            self.items = [i for i in self.items if i["summary"] != payload["item"]]
            return {}
        if path.endswith("remove_completed_items"):
            self.items = [i for i in self.items if i["status"] != sl.DONE]
            return {}
        raise AssertionError(f"unexpected HA call: {path}")

    def writes(self):
        return [p for p, _ in self.calls if "get_items" not in p]


@pytest.fixture
def ha(monkeypatch):
    fake = FakeHA()
    monkeypatch.setattr(sl, "_ha_request", fake)
    monkeypatch.setenv("HA_SHOPPING_LIST_ENTITY", ENTITY)
    return fake


class TestAdding:
    def test_a_spoken_list_becomes_separate_items(self, ha):
        result = sl.shopping_list_add("ulje, brasno, mlijeko")
        assert result["za_kupiti"] == ["ulje", "brasno", "mlijeko"]
        assert result["dodano"] == ["ulje", "brasno", "mlijeko"]

    def test_saying_it_again_does_not_duplicate(self, ha):
        sl.shopping_list_add("ulje")
        result = sl.shopping_list_add("Ulje")
        assert result["za_kupiti"] == ["ulje"]
        assert result["vec_na_listi"] == ["ulje"]
        assert result["dodano"] == []

    def test_and_is_not_a_separator(self, ha):
        """"sol i papar" is one thing you buy, not two."""
        result = sl.shopping_list_add("sol i papar")
        assert result["za_kupiti"] == ["sol i papar"]

    def test_nothing_to_add_is_an_error_not_a_write(self, ha):
        assert sl.shopping_list_add("  ")["status"] == "error"
        assert ha.writes() == []


class TestCompleting:
    def test_diacritics_and_case_do_not_matter(self, ha):
        sl.shopping_list_add("Brašno")
        result = sl.shopping_list_complete("brasno")
        assert result["promijenjeno"] == ["Brašno"]
        assert result["za_kupiti"] == []
        assert result["kupljeno"] == ["Brašno"]

    def test_a_partial_name_resolves_when_it_is_unique(self, ha):
        sl.shopping_list_add("mlijeko 2.8%")
        assert sl.shopping_list_complete("mlijeko")["promijenjeno"] == ["mlijeko 2.8%"]

    def test_two_candidates_are_never_guessed_between(self, ha):
        sl.shopping_list_add("mlijeko 2.8%, mlijeko bez laktoze")
        result = sl.shopping_list_complete("mlijeko")
        assert result["promijenjeno"] == []
        assert set(result["vise_kandidata"]["mlijeko"]) == {
            "mlijeko 2.8%",
            "mlijeko bez laktoze",
        }
        assert len(result["za_kupiti"]) == 2, "nothing may have been marked"

    def test_the_others_still_go_through(self, ha):
        """One unresolvable name must not block the rest."""
        sl.shopping_list_add("ulje, mlijeko 2.8%, mlijeko bez laktoze")
        result = sl.shopping_list_complete("ulje, mlijeko")
        assert result["promijenjeno"] == ["ulje"]
        assert "mlijeko" in result["vise_kandidata"]

    def test_something_not_on_the_list_is_reported(self, ha):
        sl.shopping_list_add("ulje")
        result = sl.shopping_list_complete("kruh")
        assert result["nije_pronadeno"] == ["kruh"]
        assert result["za_kupiti"] == ["ulje"]

    def test_it_can_be_put_back(self, ha):
        sl.shopping_list_add("ulje")
        sl.shopping_list_complete("ulje")
        result = sl.shopping_list_uncomplete("ulje")
        assert result["za_kupiti"] == ["ulje"]
        assert result["kupljeno"] == []


class TestBoughtEverythingExcept:
    def test_the_named_ones_survive(self, ha):
        sl.shopping_list_add("ulje, brasno, mlijeko, kruh")
        result = sl.shopping_list_complete_all_except("mlijeko, kruh")
        assert sorted(result["za_kupiti"]) == ["kruh", "mlijeko"]
        assert sorted(result["oznaceno_kupljeno"]) == ["brasno", "ulje"]

    def test_an_unresolvable_exception_changes_nothing(self, ha):
        """The dangerous case: complete everything and miss the exception."""
        sl.shopping_list_add("ulje, brasno, mlijeko 2.8%, mlijeko bez laktoze")
        result = sl.shopping_list_complete_all_except("mlijeko")
        assert result["status"] == "needs_clarification"
        assert len(result["za_kupiti"]) == 4, "not one item may have been marked"
        assert ha.writes() == ["/api/services/todo/add_item"] * 4

    def test_an_exception_that_is_not_on_the_list_also_stops_it(self, ha):
        sl.shopping_list_add("ulje, brasno")
        result = sl.shopping_list_complete_all_except("kruh")
        assert result["status"] == "needs_clarification"
        assert result["nije_pronadeno"] == ["kruh"]
        assert len(result["za_kupiti"]) == 2

    def test_an_empty_exception_is_refused(self, ha):
        """"kupio sam sve osim" with nothing after it must not clear the list."""
        sl.shopping_list_add("ulje")
        assert sl.shopping_list_complete_all_except("")["status"] == "error"
        assert sl.shopping_list_show()["za_kupiti"] == ["ulje"]


class TestRemovingAndTidying:
    def test_remove_takes_it_off_entirely(self, ha):
        sl.shopping_list_add("ulje, brasno")
        result = sl.shopping_list_remove("ulje")
        assert result["maknuto"] == ["ulje"]
        assert result["za_kupiti"] == ["brasno"]
        assert result["kupljeno"] == []

    def test_clearing_completed_leaves_the_open_ones(self, ha):
        sl.shopping_list_add("ulje, brasno")
        sl.shopping_list_complete("ulje")
        result = sl.shopping_list_clear_completed()
        assert result["za_kupiti"] == ["brasno"]
        assert result["kupljeno"] == []


class TestTheAnswerDescribesRealState:
    def test_every_write_reports_the_list_as_it_became(self, ha):
        """Not what was sent -- the same rule the device tools follow."""
        sl.shopping_list_add("ulje")
        ha.items.clear()  # something else emptied the list behind our back
        result = sl.shopping_list_add("brasno")
        assert result["za_kupiti"] == ["brasno"]
        assert result["broj_za_kupiti"] == 1
