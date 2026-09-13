"""
The tools that reach outside the house now need a real yes, not a promise.

Before this, exactly three MQTT switches and proposed meeting slots were gated
in code. Sending mail, sharing a document with "anyone" and deleting a calendar
entry were defended by a sentence in a prompt — the same defence that fails
when the model decides the user already agreed, and the one a prompt injection
inside a fetched page argues with directly.

Run with:
    pytest tests/unit/test_approval_gate.py -v
"""

import pytest

from services import approvals
from services.approval_gate import approval_before_tool


class _Tool:
    def __init__(self, name):
        self.name = name


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    from services import approval_gate, known_recipients

    approval_gate._holds_this_run.clear()
    approvals.reset()
    approvals.set_session("s1")
    monkeypatch.setenv("APPROVAL_TRUSTED_EMAIL_DOMAINS", "kuca.example")
    monkeypatch.setenv("KNOWN_RECIPIENTS_FILE", str(tmp_path / "known.json"))
    known_recipients.reset()
    yield
    approval_gate._holds_this_run.clear()
    approvals.reset()
    known_recipients.reset()


def _call(tool_name, **args):
    return approval_before_tool(tool=_Tool(tool_name), args=args)


class TestUngatedToolsAreUntouched:
    def test_a_read_only_tool_passes_straight_through(self):
        assert _call("calendar_list_events", max_results=5) is None

    def test_reading_mail_is_not_gated(self):
        assert _call("gmail_list_messages", query="is:unread") is None


class TestConsequentialWritesAreHeld:
    @pytest.mark.parametrize(
        "name,args",
        [
            ("calendar_delete_event", {"event_id": "E1"}),
            ("drive_share_file", {"file_id": "F1", "type": "anyone", "role": "reader"}),
            ("gmail_send_message", {"to": "stranac@example.com", "subject": "x"}),
        ],
    )
    def test_first_call_asks(self, name, args):
        result = _call(name, **args)
        assert result["status"] == "needs_confirmation"
        assert result["action"] == name

    def test_the_same_call_still_fails_without_a_user_turn(self):
        """The model cannot confirm on its own behalf by calling twice."""
        _call("calendar_delete_event", event_id="E1")
        again = _call("calendar_delete_event", event_id="E1")
        assert again["status"] == "needs_confirmation"

    def test_it_goes_through_after_the_user_says_yes(self):
        _call("calendar_delete_event", event_id="E1")
        approvals.on_user_turn("s1", affirmative=True)
        assert _call("calendar_delete_event", event_id="E1") is None

    def test_an_echoed_yes_does_not_write_twice(self):
        _call("calendar_delete_event", event_id="E1")
        approvals.on_user_turn("s1", affirmative=True)
        assert _call("calendar_delete_event", event_id="E1") is None
        assert _call("calendar_delete_event", event_id="E1") is not None

    def test_changing_the_target_needs_a_new_confirmation(self):
        """Confirming one deletion must not authorise another."""
        _call("calendar_delete_event", event_id="E1")
        approvals.on_user_turn("s1", affirmative=True)
        assert _call("calendar_delete_event", event_id="E2") is not None


class TestGmailIsGatedByUnknownRecipient:
    """Filtering by domain asked about every client — most of the real mail —
    and bought nothing: a plausible domain is the easy half of a forged
    redirect. An address nobody has ever written to is the actual signal."""

    def test_an_address_on_a_trusted_domain_goes_through(self):
        assert _call("gmail_send_message", to="ivan@kuca.example", subject="Ponuda") is None

    def test_a_known_contact_goes_through(self):
        from services import known_recipients

        known_recipients.remember(["klijent@example.com"])
        assert _call("gmail_send_message", to="klijent@example.com", subject="Ponuda") is None

    def test_a_never_seen_address_is_held(self):
        held = _call("gmail_send_message", to="stranac@example.com", subject="Ponuda")
        assert held["status"] == "needs_confirmation"
        assert "stranac@example.com" in held["question"]

    def test_one_unknown_address_among_known_ones_is_enough(self):
        held = _call(
            "gmail_send_message",
            to="ivan@kuca.example",
            cc="stranac@example.com",
            subject="Ponuda",
        )
        assert held is not None

    def test_confirming_once_teaches_the_address(self):
        _call("gmail_send_message", to="stranac@example.com", subject="Ponuda")
        approvals.on_user_turn("s1", affirmative=True)
        assert _call("gmail_send_message", to="stranac@example.com", subject="Ponuda") is None

        # A different subject to the same person no longer asks.
        assert _call("gmail_send_message", to="stranac@example.com", subject="Druga tema") is None

    def test_a_refused_send_teaches_nothing(self):
        _call("gmail_send_message", to="stranac@example.com", subject="Ponuda")
        approvals.on_user_turn("s1", affirmative=False)
        assert _call("gmail_send_message", to="stranac@example.com", subject="Ponuda") is not None

    def test_an_unreadable_store_fails_closed(self, monkeypatch, tmp_path):
        from services import known_recipients

        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("KNOWN_RECIPIENTS_FILE", str(broken))
        known_recipients.reset()

        assert _call("gmail_send_message", to="klijent@example.com", subject="x") is not None


class TestDriveIsGatedOnlyForPublishing:
    def test_sharing_with_a_person_is_ordinary_work(self):
        assert _call("drive_share_file", file_id="F1", email="ivan@kuca.example", type="user") is None

    def test_anyone_with_the_link_is_held(self):
        held = _call("drive_share_file", file_id="F1", type="anyone", role="reader")
        assert held["status"] == "needs_confirmation"


class TestFailureModes:
    def test_a_broken_rule_refuses_the_call(self, monkeypatch):
        """Fail closed. Whatever made the rule throw is precisely the input
        nobody anticipated — the last one that should run unchecked."""
        from services import approval_gate

        monkeypatch.setitem(
            approval_gate.RULES,
            "calendar_delete_event",
            approval_gate._Rule(
                when=lambda a: (_ for _ in ()).throw(RuntimeError("boom")),
                key=lambda a: {},
                question=lambda a: "",
            ),
        )
        result = _call("calendar_delete_event", event_id="E1")
        assert "APPROVAL RULE FAILED" in result["error"]

    def test_a_broken_fingerprint_refuses_the_call(self, monkeypatch):
        from services import approval_gate

        monkeypatch.setitem(
            approval_gate.RULES,
            "calendar_delete_event",
            approval_gate._Rule(
                when=lambda a: True,
                key=lambda a: (_ for _ in ()).throw(RuntimeError("boom")),
                question=lambda a: "test",
            ),
        )
        result = _call("calendar_delete_event", event_id="E1")
        assert "APPROVAL KEY FAILED" in result["error"]

    def test_an_ungated_tool_is_still_untouched_by_a_broken_rule(self, monkeypatch):
        """Failing closed must close the gated door, not every door."""
        from services import approval_gate

        monkeypatch.setitem(
            approval_gate.RULES,
            "calendar_delete_event",
            approval_gate._Rule(
                when=lambda a: (_ for _ in ()).throw(RuntimeError("boom")),
                key=lambda a: {},
                question=lambda a: "",
            ),
        )
        assert _call("mqtt_switch_control", device_name="svjetlo_kuhinja") is None

    def test_the_gate_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_GATE_ENABLED", "false")
        assert _call("calendar_delete_event", event_id="E1") is None


class TestTheReplyGoesBackToTheRightAgent:
    def test_a_deletion_question_pins_the_secretary_lane(self):
        _call("calendar_delete_event", event_id="E1")
        assert approvals.pending_lane("s1") == "secretary"

    def test_a_mail_question_pins_the_mailer_lane(self):
        _call("gmail_send_message", to="stranac@example.com", subject="x")
        assert approvals.pending_lane("s1") == "mailer"


class TestAutonomousRuns:
    """The scheduler fires at 07:30 with nobody to ask."""

    def test_a_gated_action_is_refused_not_held(self):
        approvals.set_autonomous(True)
        result = _call("gmail_send_message", to="stranac@example.com", subject="Izvještaj")

        assert result["status"] == "not_permitted"
        assert result["error"] == "AUTONOMOUS_RUN_NEEDS_CONFIRMATION"

    def test_nothing_is_left_pending_for_a_later_yes_to_arm(self):
        """Registering here is the dangerous part: a 'da' typed in Telegram
        hours later must not authorise a job's held mail."""
        approvals.set_autonomous(True)
        _call("gmail_send_message", to="stranac@example.com", subject="Izvještaj")

        assert approvals.has_pending("s1") is False
        assert approvals.has_pending("global") is False

    def test_the_model_is_told_to_stop_rather_than_retry(self):
        approvals.set_autonomous(True)
        result = _call("calendar_delete_event", event_id="E1")
        assert "Nemoj ponavljati poziv" in result["message"]

    def test_ungated_work_still_runs_autonomously(self):
        approvals.set_autonomous(True)
        assert _call("calendar_list_events", max_results=5) is None
        assert _call("gmail_send_message", to="ivan@kuca.example", subject="x") is None

    def test_interactive_runs_are_unaffected(self):
        approvals.set_autonomous(False)
        held = _call("calendar_delete_event", event_id="E1")
        assert held["status"] == "needs_confirmation"


class TestTheHeldMessageExplainsTheRelay:
    """A worker that asks for confirmation is gone by the time the user answers.
    Forwarding a bare "da" to a fresh instance loops forever without writing —
    seen on a held write, 2026-09-04."""

    def test_it_says_the_whole_request_must_be_repeated(self):
        held = _call("calendar_delete_event", event_id="E1")
        assert "CIJELI zahtjev" in held["message"]

    def test_it_warns_that_a_bare_yes_is_useless(self):
        held = _call("calendar_delete_event", event_id="E1")
        assert "'da'" in held["message"]

    def test_it_says_nothing_was_executed(self):
        held = _call("calendar_delete_event", event_id="E1")
        assert "NIJE izvršena" in held["message"]


class TestRepeatingAHeldCallInTheSameTurn:
    """Asking is something you do once.

    On 2026-09-04 a model re-issued the same held call five times inside one
    turn — five billed round-trips that could not possibly succeed, because the
    approval only arms on the user's next message and the user cannot answer
    while the turn is still running."""

    class _Ctx:
        def __init__(self, invocation_id="run-1"):
            self.invocation_id = invocation_id

    def _held(self, ctx):
        from services.approval_gate import approval_before_tool

        return approval_before_tool(
            tool=_Tool("calendar_delete_event"),
            args={"event_id": "E1"},
            tool_context=ctx,
        )

    @pytest.fixture(autouse=True)
    def _clear(self):
        from services import approval_gate

        approval_gate._holds_this_run.clear()
        yield
        approval_gate._holds_this_run.clear()

    def test_the_first_hold_asks_politely(self):
        first = self._held(self._Ctx())
        assert "Već si" not in first["message"]

    def test_the_second_tells_it_to_stop(self):
        ctx = self._Ctx()
        self._held(ctx)
        second = self._held(ctx)
        assert "PRESTANI" in second["message"]
        assert "NE MOŽE uspjeti" in second["message"]

    def test_it_counts_the_attempts(self):
        ctx = self._Ctx()
        self._held(ctx)
        second = self._held(ctx)
        assert "2 puta" in second["message"]
        # The third stops being a question at all — see
        # TestARepeatedHoldBecomesAHardStop.

    def test_a_sub_agent_call_does_not_reset_the_count(self):
        """The orchestrator calls a worker as a sub-agent, and every such call
        is its own ADK invocation. Counting per invocation made every retry
        "attempt 1" — the exact loop this counter exists to interrupt."""
        self._held(self._Ctx("invocation-1"))
        second = self._held(self._Ctx("invocation-2"))
        assert "PRESTANI" in second["message"]

    def test_a_new_user_turn_starts_over(self):
        from services.approval_gate import reset_holds

        self._held(self._Ctx())
        reset_holds("s1")  # what a new user message does
        assert "Već si" not in self._held(self._Ctx())["message"]

    def test_the_counter_clears_once_it_goes_through(self):
        from services.approval_gate import approval_before_tool

        ctx = self._Ctx()
        self._held(ctx)
        approvals.on_user_turn("s1", affirmative=True)
        assert approval_before_tool(
            tool=_Tool("calendar_delete_event"),
            args={"event_id": "E1"},
            tool_context=ctx,
        ) is None

        # A later, separate request for the same action asks politely again.
        assert "Već si" not in self._held(ctx)["message"]


class TestConsentGivenBeforeTheAttempt:
    """The gate used to decide the hold before applying a confirmation that had
    already arrived, so a yes given ahead of the attempt was recorded and then
    thrown away — a calendar deletion sat unexecuted through three turns.

    The yes is banked against the actions the gate actually asked about. It
    used to be a bare flag: a confirmation meant for one thing then armed
    whatever the model registered next, which could be something the user was
    never shown.
    """

    @staticmethod
    def _asked_then_answered(tool_name, **args):
        """Turn 1 the gate asks; turn 2 the user says yes, registration gone."""
        from services import approval_gate

        held = _call(tool_name, **args)
        assert held["status"] == "needs_confirmation"
        actions = approval_gate.take_holds("s1")

        # The pending entry has expired, so only the banked yes can help.
        approvals._PENDING.clear()
        approvals.on_user_turn("s1", affirmative=True, held_actions=actions)

    def test_a_yes_from_this_turn_lets_the_call_through(self):
        self._asked_then_answered("calendar_delete_event", event_id="E1")

        assert _call("calendar_delete_event", event_id="E1") is None

    def test_without_a_yes_it_is_still_held(self):
        approvals.on_user_turn("s1", affirmative=False)
        assert _call("calendar_delete_event", event_id="E1")["status"] == "needs_confirmation"

    def test_the_yes_does_not_cover_a_different_action(self):
        self._asked_then_answered("calendar_delete_event", event_id="E1")

        # The user confirmed a deletion. The model tries to publish a document.
        assert _call("drive_share_file", file_id="F1", type="anyone", role="reader") is not None

    def test_the_yes_does_not_cover_the_same_tool_with_other_arguments(self):
        self._asked_then_answered("calendar_delete_event", event_id="E1")

        assert _call("calendar_delete_event", event_id="E2") is not None

    def test_one_yes_still_covers_only_one_attempt(self):
        self._asked_then_answered("calendar_delete_event", event_id="E1")

        assert _call("calendar_delete_event", event_id="E1") is None
        assert _call("calendar_delete_event", event_id="E1") is not None

    def test_a_mail_let_through_this_way_still_learns_the_address(self):
        from services import known_recipients

        self._asked_then_answered(
            "gmail_send_message", to="novi@example.com", subject="x"
        )

        assert _call("gmail_send_message", to="novi@example.com", subject="x") is None
        assert known_recipients.is_known("novi@example.com") is True


class TestTheQuestionIsNotPresentedAsAFault:
    """Held once, the model told the user "naišao sam na problem — sustav traži
    potvrdu u krug", which reads as a malfunction and invites them to think
    something broke. It is the feature working."""

    def test_the_first_hold_says_it_is_not_a_fault(self):
        held = _call("calendar_delete_event", event_id="E1")
        assert "NIJE KVAR" in held["message"]

    def test_the_repeat_says_it_too(self):
        _call("calendar_delete_event", event_id="E1")
        second = _call("calendar_delete_event", event_id="E1")
        assert "NIJE KVAR" in second["message"]

    def test_it_forbids_calling_it_a_technical_problem(self):
        held = _call("drive_share_file", file_id="F9", type="anyone", role="reader")
        assert "tehnički problem" in held["message"]


class TestARepeatedHoldBecomesAHardStop:
    """Advice did not stop it: on 2026-09-04 a calendar deletion was held four
    times inside one turn, each a billed round-trip that could not succeed."""

    def _held(self, n):
        for _ in range(n):
            result = _call("calendar_delete_event", event_id="E1")
        return result

    def test_the_third_hold_returns_an_error_not_a_question(self):
        result = self._held(3)
        assert "error" in result
        assert "status" not in result

    def test_the_error_says_repeating_cannot_work(self):
        assert "NEĆE proći" in self._held(3)["error"]

    def test_it_still_forbids_calling_it_a_fault(self):
        assert "nije kvar" in self._held(3)["error"].lower()

    def test_the_first_two_are_still_questions(self):
        assert self._held(1)["status"] == "needs_confirmation"
        assert _call("calendar_delete_event", event_id="E1")["status"] == "needs_confirmation"

    def test_a_new_turn_clears_the_hard_stop(self):
        from services.approval_gate import reset_holds

        self._held(3)
        reset_holds("s1")
        assert _call("calendar_delete_event", event_id="E1")["status"] == "needs_confirmation"


class TestTheFingerprintCoversWhatTheActionDoes:
    """Recipients and subject were the whole key for a mail.

    So an approval for "mail Ana about the offer" fitted a re-issued call with
    a different body, a different attachment, and a different document shared
    along the way — all three of which are what the mail actually does.
    """

    def _approve_then_retry(self, first, second):
        """Approve `first`, then see whether `second` gets through on it."""
        from services import approval_gate

        assert _call("gmail_send_message", **first)["status"] == "needs_confirmation"
        actions = approval_gate.take_holds("s1")
        approvals._PENDING.clear()
        approvals.on_user_turn("s1", affirmative=True, held_actions=actions)
        return _call("gmail_send_message", **second)

    def test_the_same_mail_goes_through(self):
        mail = {"to": "ana@x.com", "subject": "Ponuda", "body": "U prilogu."}
        assert self._approve_then_retry(mail, dict(mail)) is None

    def test_a_different_body_does_not(self):
        held = self._approve_then_retry(
            {"to": "ana@x.com", "subject": "Ponuda", "body": "U prilogu."},
            {"to": "ana@x.com", "subject": "Ponuda", "body": "Otkazujem sve."},
        )
        assert held is not None

    def test_a_different_attachment_does_not(self, tmp_path):
        first = tmp_path / "ponuda.pdf"
        first.write_bytes(b"prava ponuda")

        base = {"to": "ana@x.com", "subject": "Ponuda", "body": "U prilogu.",
                "attachment_path": str(first)}

        # The path is the same; the file is not. Hashing the name would have
        # let this through.
        def swap():
            first.write_bytes(b"nesto sasvim drugo")
            return dict(base)

        from services import approval_gate

        assert _call("gmail_send_message", **base)["status"] == "needs_confirmation"
        actions = approval_gate.take_holds("s1")
        approvals._PENDING.clear()
        approvals.on_user_turn("s1", affirmative=True, held_actions=actions)

        assert _call("gmail_send_message", **swap()) is not None

    def test_a_different_linked_document_does_not(self):
        link = "https://docs.google.com/document/d/{}/edit"
        held = self._approve_then_retry(
            {"to": "ana@x.com", "subject": "Ponuda",
             "body": link.format("DOC_PONUDA_1234567890")},
            {"to": "ana@x.com", "subject": "Ponuda",
             "body": link.format("DOC_PLACE_0987654321")},
        )
        # Sending a link is not the same intent as changing who may open a
        # document, and the mailer does the second on its way to the first.
        assert held is not None


class TestTheQuestionNamesTheDocumentsBeingShared:

    def test_shared_documents_are_spelled_out(self):
        held = _call(
            "gmail_send_message",
            to="ana@x.com",
            subject="Ponuda",
            body="https://docs.google.com/document/d/DOC_PONUDA_1234567890/edit",
        )

        question = held["question"]
        assert "DOC_PONUDA_1234567890" in question
        assert "pristup" in question
        assert "ana@x.com" in question

    def test_a_mail_without_links_is_asked_about_plainly(self):
        held = _call("gmail_send_message", to="ana@x.com", subject="Ponuda",
                     body="Nema linkova.")

        assert "pristup" not in held["question"]
