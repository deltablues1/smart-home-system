"""
Sending a link and changing who may open the document are not the same intent.

The mailer does the second on its way to the first: before sending, it grants
every recipient read access to any Drive file the body links. That is genuinely
useful — the alternative was making documents public — but it used to be
invisible and irreversible.

* The permission ids were thrown away, with a comment saying `drive_share_file`
  did not return them. It does, on the line that asks for them.
* So a send that failed left the access behind, and said so in a warning that
  offered nothing to do about it.
* And access already held by a recipient was indistinguishable from access this
  send created, which is why taking anything back was unsafe.

Now the grants are recorded, recipients who already had access are skipped, and
the access is taken back only when the send provably did not happen. A lost
answer is not proof: revoking then takes a document away from someone who is
already reading the mail.

No Google. The Drive and Gmail implementations are stubbed.

Run with:
    pytest tests/unit/test_gmail_autoshare.py -v
"""

import pytest

import tools.adk_tools.gmail_adk_tools as gmail_adk
import tools.api_implementations.drive_api as drive_api
import tools.api_implementations.gmail_api as gmail_api

DOC = "https://docs.google.com/document/d/DOC_PONUDA_1234567890/edit"
FILE_ID = "DOC_PONUDA_1234567890"


@pytest.fixture
def drive(monkeypatch):
    """A stand-in Drive that remembers who can open what."""
    state = {
        "permissions": {},          # file_id -> {email: permission_id}
        "granted": [],
        "revoked": [],
        "list_fails": False,
        "next_id": iter(f"perm-{n}" for n in range(1, 100)),
    }

    async def list_permissions(creds, file_id):
        if state["list_fails"]:
            raise RuntimeError("permissions unreadable")
        return {
            "file_id": file_id,
            "permissions": [
                {"id": pid, "emailAddress": email, "role": "reader"}
                for email, pid in state["permissions"].get(file_id, {}).items()
            ],
        }

    async def share(creds, file_id, email, role, type_, send_notification=True):
        permission_id = next(state["next_id"])
        state["permissions"].setdefault(file_id, {})[email] = permission_id
        state["granted"].append((file_id, email))
        return {"file_id": file_id, "permission_id": permission_id}

    async def revoke(creds, file_id, permission_id):
        state["revoked"].append((file_id, permission_id))
        for email, pid in list(state["permissions"].get(file_id, {}).items()):
            if pid == permission_id:
                del state["permissions"][file_id][email]
        return {"status": "revoked"}

    monkeypatch.setattr(drive_api, "drive_list_permissions", list_permissions)
    monkeypatch.setattr(drive_api, "drive_share_file", share)
    monkeypatch.setattr(drive_api, "drive_revoke_permission", revoke)
    monkeypatch.setattr(gmail_adk, "_get_credentials", lambda: object())
    return state


def _http_error(status):
    """A real googleapiclient HttpError, which is what proof looks like."""
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "test"

    return HttpError(_Resp(status), b'{"error": "test"}', uri="https://gmail.test")


def _sending(monkeypatch, outcome):
    async def send(*args, **kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(gmail_api, "gmail_send_message", send)


async def _send(**overrides):
    args = {"to": "ana@x.com", "subject": "Ponuda", "body": DOC}
    args.update(overrides)
    return await gmail_adk.gmail_send_message(**args)


class TestWhatGetsShared:

    @pytest.mark.asyncio
    async def test_a_linked_document_is_shared_with_the_recipient(
        self, drive, monkeypatch
    ):
        _sending(monkeypatch, {"status": "sent", "message_id": "m1"})

        await _send()

        assert drive["granted"] == [(FILE_ID, "ana@x.com")]

    @pytest.mark.asyncio
    async def test_someone_who_already_has_access_is_left_alone(
        self, drive, monkeypatch
    ):
        drive["permissions"][FILE_ID] = {"ana@x.com": "perm-existing"}
        _sending(monkeypatch, {"status": "sent"})

        await _send()

        # Nothing was granted, so nothing here is ours to take away later.
        assert drive["granted"] == []


class TestTakingAccessBack:

    @pytest.mark.asyncio
    async def test_a_refused_send_gives_the_access_back(self, drive, monkeypatch):
        _sending(monkeypatch, {"status": "failed", "error": "Invalid recipient"})

        result = await _send()

        assert drive["revoked"] == [(FILE_ID, "perm-1")]
        assert "povukao pristup" in result["share_warning"]

    @pytest.mark.asyncio
    async def test_a_rejection_exception_does_too(self, drive, monkeypatch):
        _sending(monkeypatch, _http_error(400))

        result = await _send()

        assert drive["revoked"] == [(FILE_ID, "perm-1")]
        assert result.get("outcome") != "unknown"

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_not_a_rejection(self, drive, monkeypatch):
        # 429 is "later", which is not the same as "no".
        _sending(monkeypatch, _http_error(429))

        result = await _send()

        assert drive["revoked"] == []
        assert result["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_a_server_error_is_not_a_rejection(self, drive, monkeypatch):
        _sending(monkeypatch, _http_error(500))

        result = await _send()

        assert drive["revoked"] == []
        assert result["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_an_unparseable_reply_keeps_the_access(self, drive, monkeypatch):
        # Gmail sent the mail and the answer would not parse. Asking "is this
        # a known transient error" said no, and no used to mean revoke — so
        # the document vanished from under someone already reading about it.
        import json

        _sending(monkeypatch, json.JSONDecodeError("Expecting value", "", 0))

        result = await _send()

        assert drive["revoked"] == []
        assert result["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_a_missing_field_after_a_send_keeps_it_too(
        self, drive, monkeypatch
    ):
        _sending(monkeypatch, KeyError("id"))

        result = await _send()

        assert drive["revoked"] == []
        assert result["outcome"] == "unknown"

    @pytest.mark.asyncio
    async def test_a_lost_answer_keeps_the_access(self, drive, monkeypatch):
        _sending(monkeypatch, TimeoutError("The read operation timed out"))

        result = await _send()

        # The mail may already be in the recipient's inbox. Revoking here
        # takes the document away from someone who is reading about it.
        assert drive["revoked"] == []
        assert result["outcome"] == "unknown"
        assert "NISAM povukao" in result["share_warning"]

    @pytest.mark.asyncio
    async def test_a_successful_send_keeps_it_too(self, drive, monkeypatch):
        _sending(monkeypatch, {"status": "sent", "message_id": "m1"})

        result = await _send()

        assert drive["revoked"] == []
        assert "povukao" not in result.get("share_warning", "")

    @pytest.mark.asyncio
    async def test_access_that_was_not_ours_is_never_revoked(
        self, drive, monkeypatch
    ):
        drive["permissions"][FILE_ID] = {"ana@x.com": "perm-existing"}
        _sending(monkeypatch, {"status": "failed", "error": "Invalid recipient"})

        await _send()

        assert drive["revoked"] == []
        assert drive["permissions"][FILE_ID] == {"ana@x.com": "perm-existing"}

    @pytest.mark.asyncio
    async def test_unreadable_permissions_mean_no_rollback(self, drive, monkeypatch):
        # If the current access cannot be read, ours cannot be told from
        # theirs afterwards — so share, but never offer to undo.
        drive["list_fails"] = True
        _sending(monkeypatch, {"status": "failed", "error": "Invalid recipient"})

        await _send()

        assert drive["granted"] == [(FILE_ID, "ana@x.com")]
        assert drive["revoked"] == []
