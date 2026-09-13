"""
Who the house has written to before.

The approval gate needs this answer synchronously, inside a tool callback, so
it cannot call the People API. The set is seeded from Contacts at startup and
grows every time a send is confirmed, which narrows the gate down to addresses
nobody has ever written to — the shape of a redirect injected into fetched
content, and not the shape of a client you email weekly.

Run with:
    pytest tests/unit/test_known_recipients.py -v
"""

import json

import pytest

from services import known_recipients as kr


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWN_RECIPIENTS_FILE", str(tmp_path / "known.json"))
    monkeypatch.setenv("APPROVAL_TRUSTED_EMAIL_DOMAINS", "kuca.example")
    kr.reset()
    yield tmp_path / "known.json"
    kr.reset()


class TestKnowing:
    def test_a_new_address_is_unknown(self):
        assert kr.is_known("stranac@example.com") is False

    def test_a_trusted_domain_needs_no_learning(self):
        assert kr.is_known("bilo.tko@kuca.example") is True

    def test_remembering_makes_it_known(self):
        kr.remember(["klijent@example.com"])
        assert kr.is_known("klijent@example.com") is True

    def test_case_and_angle_brackets_do_not_matter(self):
        kr.remember(["Klijent@Example.COM"])
        assert kr.is_known("<klijent@example.com>") is True

    def test_nonsense_is_never_known(self):
        assert kr.is_known("") is False
        assert kr.is_known("not-an-address") is False


class TestPersistence:
    def test_it_survives_a_restart(self, store):
        kr.remember(["klijent@example.com"])
        kr.reset()  # as if the process restarted
        assert kr.is_known("klijent@example.com") is True

    def test_the_file_is_readable_json(self, store):
        kr.remember(["klijent@example.com"])
        data = json.loads(store.read_text(encoding="utf-8"))
        assert data["addresses"] == ["klijent@example.com"]

    def test_remember_reports_only_what_was_new(self):
        assert kr.remember(["a@b.com"]) == 1
        assert kr.remember(["a@b.com"]) == 0


class TestFailingClosed:
    def test_a_corrupt_store_makes_everything_unknown(self, store):
        store.write_text("{not json", encoding="utf-8")
        kr.reset()
        assert kr.is_known("klijent@example.com") is False

    def test_a_missing_store_makes_everything_unknown(self):
        assert kr.count() == 0
        assert kr.is_known("klijent@example.com") is False


class TestContactsSeeding:
    @pytest.mark.asyncio
    async def test_contacts_are_learned(self, monkeypatch):
        async def fake_list(credentials, page_size=1000):
            return {"contacts": [
                {"name": "Ivan", "email": "ivan@klijent.hr"},
                {"name": "Ana", "emails": ["ana@drugi.hr", "ana.b@drugi.hr"]},
                {"name": "Bez maila"},
            ]}

        import tools.api_implementations.contacts_api as contacts_api
        monkeypatch.setattr(contacts_api, "contacts_list_contacts", fake_list)

        added = await kr.refresh_from_contacts(credentials=object())

        assert added == 3
        assert kr.is_known("ivan@klijent.hr") is True
        assert kr.is_known("ana.b@drugi.hr") is True

    @pytest.mark.asyncio
    async def test_contacts_being_down_is_not_fatal(self, monkeypatch):
        async def boom(credentials, page_size=1000):
            raise RuntimeError("People API unavailable")

        import tools.api_implementations.contacts_api as contacts_api
        monkeypatch.setattr(contacts_api, "contacts_list_contacts", boom)

        assert await kr.refresh_from_contacts(credentials=object()) == 0
