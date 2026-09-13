"""
A write that may already have happened must never be repeated silently.

`with_retry` fires on 429 and 5xx and on client timeouts — exactly the cases
where Google may have applied the write and only the answer got lost. On an
append or a create that means a duplicate row, a duplicate document, a
duplicate task. `gmail_send_message` and `calendar_create_event` had the
decorator removed for this reason; this file holds the rest of the audit, and
guards it against coming back.

Dropping the retry is only half of it. The agent reads a raw "connection
reset" as a plain failure and calls the tool again itself, so the duplicate
returns one level up. `report_unconfirmed` turns those transient errors into
an explicit "NIJE POTVRĐENO", which tells the model to check before repeating.

Functions that keep their retry are listed too, so the decision is visible
rather than implied by a name: `docs_replace_text` sounds idempotent and is
not (A -> AA run twice gives AAAA), while `drive_delete_file` sounds
destructive and is.

No network, no Google.

Run with:
    pytest tests/unit/test_write_retry_safety.py -v
"""

import inspect

import pytest

from tools.resilience.retry_handler import (
    UnconfirmedWrite,
    is_retryable_error,
    report_unconfirmed,
)


# (module path, function name) for every write that must NOT be retried.
NON_IDEMPOTENT = [
    ("gmail_api", "gmail_send_message"),
    ("gmail_api", "gmail_create_draft"),
    ("calendar_api", "calendar_create_event"),
    ("sheets_api", "sheets_create_spreadsheet"),
    ("sheets_api", "sheets_append_values"),
    ("sheets_api", "sheets_batch_update"),
    ("drive_api", "drive_upload_file"),
    ("drive_api", "drive_create_folder"),
    ("docs_api", "docs_create_document"),
    ("docs_api", "docs_insert_text"),
    ("docs_api", "docs_append_text"),
    ("docs_api", "docs_batch_update"),
    ("docs_api", "docs_replace_text"),
    ("tasks_api", "tasks_create_task"),
    ("contacts_api", "contacts_create_contact"),
]

# Writes that keep a retry, with the reason recorded in the module comment.
IDEMPOTENT = [
    ("sheets_api", "sheets_update_values"),
    ("sheets_api", "sheets_clear_values"),
    ("drive_api", "drive_update_file"),
    ("drive_api", "drive_delete_file"),
    ("drive_api", "drive_share_file"),
    ("drive_api", "drive_move_file"),
    ("docs_api", "docs_format_text"),
    ("tasks_api", "tasks_update_task"),
    ("tasks_api", "tasks_delete_task"),
    ("contacts_api", "contacts_update_contact"),
    ("contacts_api", "contacts_delete_contact"),
]


def _decorators(module_name, func_name):
    module = __import__(
        f"tools.api_implementations.{module_name}", fromlist=[func_name]
    )
    source = inspect.getsource(getattr(module, func_name))
    head = source.split("async def", 1)[0]
    return [line.strip() for line in head.splitlines() if line.strip().startswith("@")]


class TestNonIdempotentWritesAreNotRetried:

    @pytest.mark.parametrize("module_name,func_name", NON_IDEMPOTENT)
    def test_no_retry_decorator(self, module_name, func_name):
        decorators = _decorators(module_name, func_name)
        assert not any(
            d.startswith("@with_retry") or d.startswith("@with_quota_retry")
            for d in decorators
        ), f"{func_name} would duplicate the write on a retry"

    @pytest.mark.parametrize("module_name,func_name", NON_IDEMPOTENT)
    def test_a_lost_answer_is_reported_not_swallowed(self, module_name, func_name):
        # gmail_send_message and calendar_create_event predate the helper and
        # report through their own result dict; the rest carry the decorator.
        if func_name in ("gmail_send_message", "calendar_create_event"):
            pytest.skip("reports the unknown outcome through its own result")
        decorators = _decorators(module_name, func_name)
        assert any(d.startswith("@report_unconfirmed(") for d in decorators), (
            f"{func_name} drops the retry but still hands the agent a raw error, "
            f"which it will read as a plain failure and repeat"
        )


class TestIdempotentWritesKeepTheirRetry:

    @pytest.mark.parametrize("module_name,func_name", IDEMPOTENT)
    def test_retry_is_still_there(self, module_name, func_name):
        decorators = _decorators(module_name, func_name)
        assert any(d.startswith("@with_retry") for d in decorators), (
            f"{func_name} is safe to repeat; losing the retry only makes it "
            f"fail more often"
        )

    @pytest.mark.parametrize("module_name,func_name", IDEMPOTENT)
    def test_the_reason_is_written_down(self, module_name, func_name):
        module = __import__(
            f"tools.api_implementations.{module_name}", fromlist=[func_name]
        )
        source = inspect.getsource(getattr(module, func_name))
        head = source.split("async def", 1)[0]
        assert "Retry is safe:" in head, (
            f"{func_name} keeps a retry with no recorded reason — the next "
            f"audit has to re-derive it from the name, which is how "
            f"docs_replace_text nearly kept one"
        )


class TestReportUnconfirmed:

    @pytest.mark.asyncio
    async def test_transient_error_becomes_an_unconfirmed_write(self):
        @report_unconfirmed("dodavanje redaka")
        async def flaky():
            raise TimeoutError("The read operation timed out")

        with pytest.raises(UnconfirmedWrite) as excinfo:
            await flaky()

        message = str(excinfo.value)
        assert "NIJE POTVRĐENO" in message
        assert "dodavanje redaka" in message
        # The agent must be told to look before leaping.
        assert "Provjeri" in message

    @pytest.mark.asyncio
    async def test_permanent_error_passes_through(self):
        class Rejected(Exception):
            pass

        @report_unconfirmed("stvaranje dokumenta")
        async def rejected():
            raise Rejected("Invalid document id")

        assert is_retryable_error(Rejected("Invalid document id")) is False
        with pytest.raises(Rejected):
            await rejected()

    @pytest.mark.asyncio
    async def test_success_is_untouched(self):
        @report_unconfirmed("stvaranje zadatka")
        async def fine():
            return {"status": "created", "id": "abc"}

        assert await fine() == {"status": "created", "id": "abc"}

    @pytest.mark.asyncio
    async def test_it_does_not_retry(self):
        calls = []

        @report_unconfirmed("upload datoteke")
        async def counts():
            calls.append(1)
            raise TimeoutError("connection reset")

        with pytest.raises(UnconfirmedWrite):
            await counts()

        assert len(calls) == 1, "report_unconfirmed must never call twice"


class TestTheUnknownOutcomeReachesTheAgent:
    """The message is only useful if the shape agrees with it.

    The ADK wrappers catch everything and return `status="failed"`, which reads
    as "nothing happened" — the opposite of what an unconfirmed write means,
    and an invitation to call the tool again.
    """

    @pytest.mark.asyncio
    async def test_append_reports_unknown_not_failed(self, monkeypatch):
        import tools.adk_tools.sheets_adk_tools as sheets_adk
        import tools.api_implementations.sheets_api as sheets_api

        monkeypatch.setattr(sheets_adk, "_get_credentials", lambda: object())

        async def lost_answer(*args, **kwargs):
            raise UnconfirmedWrite("dodavanje redaka: NIJE POTVRĐENO ...")

        monkeypatch.setattr(sheets_api, "sheets_append_values", lost_answer)

        result = await sheets_adk.sheets_append_values(
            "sheet-id", "Sheet1", [["a", "b"]]
        )

        assert result["status"] == "unknown"
        assert result["outcome"] == "unknown"
        assert result["status"] != "failed"

    @pytest.mark.asyncio
    async def test_a_permanent_error_is_still_a_failure(self, monkeypatch):
        import tools.adk_tools.sheets_adk_tools as sheets_adk
        import tools.api_implementations.sheets_api as sheets_api

        monkeypatch.setattr(sheets_adk, "_get_credentials", lambda: object())

        async def rejected(*args, **kwargs):
            raise ValueError("Invalid range")

        monkeypatch.setattr(sheets_api, "sheets_append_values", rejected)

        result = await sheets_adk.sheets_append_values(
            "sheet-id", "Nope!A1", [["a"]]
        )

        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_a_second_call_is_not_blocked_yet(self):
        # Recorded deliberately: report_unconfirmed removes the AUTOMATIC
        # retry and keeps the outcome structured. It does not know that two
        # calls are the same operation, so it cannot stop the model from
        # trying again. That needs an operation identity and a pending state,
        # and it belongs with the step-result contract — not here. Until then
        # this is a signal, not a guard, and the test says so out loud.
        calls = []

        @report_unconfirmed("dodavanje redaka")
        async def write():
            calls.append(1)
            raise TimeoutError("timed out")

        for _ in range(2):
            with pytest.raises(UnconfirmedWrite):
                await write()

        assert len(calls) == 2
