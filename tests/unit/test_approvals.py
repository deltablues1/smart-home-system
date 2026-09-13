"""
The gate that has to hold when the model is wrong about what the user said.

Every property here exists because the prompt-only version failed at it: a
model acting in the turn it asked, an echoed "da" executing twice, one "da"
authorising a queue of pending writes, and a turn in one session arming a
pending action from another.

Run with:
    pytest tests/unit/test_approvals.py -v
"""

import pytest

from services import approvals


@pytest.fixture(autouse=True)
def clean():
    approvals.reset()
    approvals.set_session("s1")
    yield
    approvals.reset()


class TestSameTurnExecutionIsImpossible:
    def test_a_fresh_approval_cannot_be_redeemed(self):
        approvals.register("mqtt:bojler-off")
        assert approvals.redeem("mqtt:bojler-off") is False

    def test_it_works_after_the_user_speaks_again(self):
        approvals.register("mqtt:bojler-off")
        approvals.on_user_turn("s1", affirmative=True)
        assert approvals.redeem("mqtt:bojler-off") is True


class TestConsumeOnUse:
    def test_an_echoed_yes_cannot_execute_twice(self):
        approvals.register("calendar:delete-E5")
        approvals.on_user_turn("s1", affirmative=True)
        assert approvals.redeem("calendar:delete-E5") is True
        assert approvals.redeem("calendar:delete-E5") is False


class TestOneYesAuthorisesOneAction:
    def test_only_the_newest_question_is_armed(self):
        approvals.register("mqtt:bojler-off", question="Ugasiti bojler?")
        approvals.register("mqtt:pecnica-on", question="Upaliti pećnicu?")
        approvals.on_user_turn("s1", affirmative=True)

        assert approvals.redeem("mqtt:pecnica-on") is True
        assert approvals.redeem("mqtt:bojler-off") is False

    def test_the_armed_action_is_reported_back(self):
        approvals.register("a")
        approvals.register("b")
        assert approvals.on_user_turn("s1", affirmative=True) == "b"


class TestARefusalCancels:
    def test_a_non_yes_drops_pending_approvals(self):
        approvals.register("mqtt:bojler-off")
        approvals.on_user_turn("s1", affirmative=False)
        approvals.on_user_turn("s1", affirmative=True)
        assert approvals.redeem("mqtt:bojler-off") is False


class TestSessionScoping:
    def test_another_session_cannot_arm_your_action(self):
        approvals.register("mqtt:bojler-off", session="s1")
        approvals.on_user_turn("s2", affirmative=True)
        assert approvals.redeem("mqtt:bojler-off", session="s1") is False

    def test_redeeming_uses_the_bound_session(self):
        approvals.register("x", session="s1")
        approvals.on_user_turn("s1", affirmative=True)
        approvals.set_session("s2")
        assert approvals.redeem("x") is False
        approvals.set_session("s1")
        assert approvals.redeem("x") is True


class TestExpiry:
    def test_an_expired_approval_is_not_redeemable(self):
        approvals.register("slow", ttl=0.0)
        approvals.on_user_turn("s1", affirmative=True)
        assert approvals.redeem("slow") is False

    def test_expired_entries_do_not_count_as_pending(self):
        approvals.register("slow", ttl=0.0)
        assert approvals.has_pending("s1") is False


class TestProposalMode:
    """Choosing a meeting slot is the confirmation; it is not the word "da"."""

    def test_any_next_turn_arms_a_proposal(self):
        approvals.register("cal:slot-1", arm_mode=approvals.NEXT_TURN)
        approvals.on_user_turn("s1", affirmative=False)
        assert approvals.redeem("cal:slot-1") is True

    def test_all_slots_proposed_together_are_armed(self):
        for i in range(3):
            approvals.register(f"cal:slot-{i}", arm_mode=approvals.NEXT_TURN)
        approvals.on_user_turn("s1", affirmative=False)
        assert all(approvals.redeem(f"cal:slot-{i}") for i in range(3))

    def test_still_not_redeemable_in_the_proposing_turn(self):
        approvals.register("cal:slot-1", arm_mode=approvals.NEXT_TURN)
        assert approvals.redeem("cal:slot-1") is False


class TestFingerprint:
    def test_same_action_same_id(self):
        a = approvals.fingerprint("calendar_move_event", event="E1", shift=-5)
        b = approvals.fingerprint("calendar_move_event", shift=-5, event="E1")
        assert a == b

    def test_a_changed_amount_is_a_different_action(self):
        five = approvals.fingerprint("calendar_move_event", event="E1", shift=-5)
        fifty = approvals.fingerprint("calendar_move_event", event="E1", shift=-50)
        assert five != fifty

    def test_the_tool_name_is_readable_in_the_id(self):
        assert approvals.fingerprint("gmail_send_message", to="a@b.c").startswith(
            "gmail_send_message:"
        )


class TestPendingQuestion:
    def test_reports_the_most_recent_question(self):
        approvals.register("a", question="Ugasiti bojler?")
        approvals.register("b", question="Upaliti pećnicu?")
        assert approvals.pending_question("s1") == "Upaliti pećnicu?"

    def test_none_when_nothing_is_waiting(self):
        assert approvals.pending_question("s1") is None


class TestAffirmativeParsing:
    """A sentence that starts with "da" is not consent if it takes it back."""

    @pytest.mark.parametrize(
        "reply",
        ["da", "moze", "u redu", "ok", "potvrdujem", "da naravno", "tako je"],
    )
    def test_plain_yes(self, reply):
        from interfaces.base_interface import BaseInterface

        assert BaseInterface._is_affirmative_reply(reply) is True

    @pytest.mark.parametrize(
        "reply",
        [
            "da ali nemoj",
            "da ne gasi bojler",
            "ne",
            "nemoj",
            "da ipak ne",
            "cekaj",
            "koliko je sati",
        ],
    )
    def test_not_consent(self, reply):
        from interfaces.base_interface import BaseInterface

        assert BaseInterface._is_affirmative_reply(reply) is False

    def test_a_long_sentence_starting_with_da_is_not_a_confirmation(self):
        from interfaces.base_interface import BaseInterface

        assert BaseInterface._is_affirmative_reply(
            "da bih volio znati koliko to sve skupa kosta"
        ) is False


class TestLaneRouting:
    def test_the_reply_goes_back_to_the_agent_that_asked(self):
        approvals.register("calendar:delete", lane="secretary", question="Obrisati termin?")
        assert approvals.pending_lane("s1") == "secretary"

    def test_no_lane_when_nothing_is_waiting(self):
        assert approvals.pending_lane("s1") is None


class TestConfirmationThatArrivesBeforeTheAttempt:
    """A gate that only ever arms on the NEXT message spends a whole turn
    whenever the model asks its own clarifying question first. Deleting a
    calendar event took three turns on 2026-09-04 because turn one went on
    identifying which event. The consent was real; it just arrived early."""

    def test_a_yes_authorises_the_action_attempted_in_that_turn(self):
        # "held_actions" is what the gate stopped to ask about last turn.
        approvals.on_user_turn(
            "s1", affirmative=True, held_actions={"cal:delete-1"}
        )
        approvals.register("cal:delete-1")               # model tries afterwards
        assert approvals.redeem("cal:delete-1") is True

    def test_it_authorises_only_the_action_that_was_asked_about(self):
        # The yes used to be banked for whatever came next, so a confirmation
        # meant for one thing could authorise something the user was never
        # shown. Now it only fits the question that was asked.
        approvals.on_user_turn(
            "s1", affirmative=True, held_actions={"cal:delete-1"}
        )
        approvals.register("calendar:delete-E1")
        approvals.register("cal:delete-1")

        assert approvals.redeem("calendar:delete-E1") is False
        assert approvals.redeem("cal:delete-1") is True

    def test_it_authorises_only_one_attempt_of_it(self):
        approvals.on_user_turn(
            "s1", affirmative=True, held_actions={"cal:delete-1"}
        )
        approvals.register("cal:delete-1")
        assert approvals.redeem("cal:delete-1") is True

        approvals.register("cal:delete-1")
        assert approvals.redeem("cal:delete-1") is False

    def test_a_yes_with_nothing_asked_authorises_nothing(self):
        approvals.on_user_turn("s1", affirmative=True)   # gate held nothing
        approvals.register("cal:delete-1")
        assert approvals.redeem("cal:delete-1") is False

    def test_a_non_affirmative_turn_authorises_nothing(self):
        approvals.on_user_turn("s1", affirmative=False)
        approvals.register("cal:delete-1")
        assert approvals.redeem("cal:delete-1") is False

    def test_the_model_still_cannot_act_without_any_user_message(self):
        approvals.register("cal:delete-1")
        assert approvals.redeem("cal:delete-1") is False

    def test_an_echoed_yes_cannot_repeat_what_just_ran(self):
        approvals.on_user_turn(
            "s1", affirmative=True, held_actions={"calendar:delete-E5"}
        )
        approvals.register("calendar:delete-E5")
        assert approvals.redeem("calendar:delete-E5") is True

        approvals.on_user_turn(                          # the echo
            "s1", affirmative=True, held_actions={"calendar:delete-E5"}
        )
        approvals.register("calendar:delete-E5")
        assert approvals.redeem("calendar:delete-E5") is False

    def test_a_pending_question_still_takes_precedence(self):
        """When something IS waiting, the yes belongs to it, not to whatever
        the model tries next."""
        approvals.register("mqtt:bojler-off")
        approvals.on_user_turn("s1", affirmative=True)
        approvals.register("calendar:delete-E1")

        assert approvals.redeem("calendar:delete-E1") is False
        assert approvals.redeem("mqtt:bojler-off") is True

    def test_it_can_be_switched_off(self, monkeypatch):
        monkeypatch.setenv("APPROVAL_ALLOW_SAME_TURN", "false")
        approvals.on_user_turn(
            "s1", affirmative=True, held_actions={"cal:delete-1"}
        )
        approvals.register("cal:delete-1")
        assert approvals.redeem("cal:delete-1") is False

    def test_slot_proposals_are_unaffected(self):
        approvals.on_user_turn("s1", affirmative=True)
        approvals.register("cal:slot-1", arm_mode=approvals.NEXT_TURN)
        assert approvals.redeem("cal:slot-1") is False
