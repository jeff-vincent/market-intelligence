"""Tests for api-server pure functions and route handlers."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

# Import the module under test
import main as api


# ────────────────────────────────────────────────────────────────────────────
# mask_key
# ────────────────────────────────────────────────────────────────────────────

class TestMaskKey:
    def test_long_key(self):
        assert api.mask_key("sk_test_abc123456789xyz") == "sk_t...9xyz"

    def test_short_key(self):
        assert api.mask_key("short") == "****"

    def test_exactly_10(self):
        assert api.mask_key("0123456789") == "****"

    def test_eleven_chars(self):
        assert api.mask_key("01234567890") == "0123...7890"

    def test_empty(self):
        assert api.mask_key("") == "****"


# ────────────────────────────────────────────────────────────────────────────
# json_serial
# ────────────────────────────────────────────────────────────────────────────

class TestJsonSerial:
    def test_objectid(self):
        oid = ObjectId("507f1f77bcf86cd799439011")
        assert api.json_serial(oid) == "507f1f77bcf86cd799439011"

    def test_datetime(self):
        dt = datetime(2026, 3, 12, 10, 30, 0, tzinfo=timezone.utc)
        result = api.json_serial(dt)
        assert "2026-03-12" in result

    def test_unsupported_type(self):
        with pytest.raises(TypeError):
            api.json_serial(set())


# ────────────────────────────────────────────────────────────────────────────
# _extract_reddit_thing_id
# ────────────────────────────────────────────────────────────────────────────

class TestExtractRedditThingId:
    def test_standard_post_url(self):
        url = "https://www.reddit.com/r/kubernetes/comments/abc123/some_post_title/"
        assert api._extract_reddit_thing_id(url) == "t3_abc123"

    def test_comment_url(self):
        url = "https://www.reddit.com/r/devops/comments/xyz789/my_post/comment_id/"
        assert api._extract_reddit_thing_id(url) == "t3_xyz789"

    def test_old_reddit(self):
        url = "https://old.reddit.com/r/selfhosted/comments/def456/title/"
        assert api._extract_reddit_thing_id(url) == "t3_def456"

    def test_no_comments_in_url(self):
        with pytest.raises(ValueError):
            api._extract_reddit_thing_id("https://www.reddit.com/r/kubernetes/")

    def test_empty_url(self):
        with pytest.raises(ValueError):
            api._extract_reddit_thing_id("")


# ────────────────────────────────────────────────────────────────────────────
# _xml_escape
# ────────────────────────────────────────────────────────────────────────────

class TestXmlEscape:
    def test_ampersand(self):
        assert api._xml_escape("A & B") == "A &amp; B"

    def test_angle_brackets(self):
        assert api._xml_escape("<b>bold</b>") == "&lt;b&gt;bold&lt;/b&gt;"

    def test_quotes(self):
        assert api._xml_escape('say "hello"') == "say &quot;hello&quot;"

    def test_no_escaping_needed(self):
        assert api._xml_escape("plain text") == "plain text"

    def test_combined(self):
        assert api._xml_escape('<a href="x">&</a>') == '&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;'


# ────────────────────────────────────────────────────────────────────────────
# json_response
# ────────────────────────────────────────────────────────────────────────────

class TestJsonResponse:
    def test_basic_dict(self):
        resp = api.json_response({"ok": True})
        assert resp.status == 200
        assert resp.content_type == "application/json"
        body = json.loads(resp.text)
        assert body == {"ok": True}

    def test_custom_status(self):
        resp = api.json_response({"error": "bad"}, status=400)
        assert resp.status == 400

    def test_serializes_objectid(self):
        oid = ObjectId("507f1f77bcf86cd799439011")
        resp = api.json_response({"_id": oid})
        body = json.loads(resp.text)
        assert body["_id"] == "507f1f77bcf86cd799439011"


# ────────────────────────────────────────────────────────────────────────────
# get_user_id
# ────────────────────────────────────────────────────────────────────────────

class TestGetUserId:
    def test_with_user_id(self):
        request = MagicMock()
        request._user_id = "auth0|abc123"
        assert api.get_user_id(request) == "auth0|abc123"

    def test_anonymous_fallback(self):
        request = MagicMock(spec=[])  # no _user_id attribute
        assert api.get_user_id(request) == "anonymous"


# ────────────────────────────────────────────────────────────────────────────
# get_fernet
# ────────────────────────────────────────────────────────────────────────────

class TestGetFernet:
    def test_no_key(self):
        with patch.object(api, "ENCRYPTION_KEY", ""):
            assert api.get_fernet() is None

    def test_valid_key(self):
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        with patch.object(api, "ENCRYPTION_KEY", key):
            f = api.get_fernet()
            assert f is not None
            # Verify round-trip
            ct = f.encrypt(b"hello")
            assert f.decrypt(ct) == b"hello"

    def test_invalid_key(self):
        with patch.object(api, "ENCRYPTION_KEY", "not-a-valid-key"):
            assert api.get_fernet() is None


# ────────────────────────────────────────────────────────────────────────────
# SUPPORTED_INTEGRATIONS structure
# ────────────────────────────────────────────────────────────────────────────

class TestSupportedIntegrations:
    def test_all_types_present(self):
        expected = {"slack", "discord", "email", "webhook", "notion", "linear"}
        assert set(api.SUPPORTED_INTEGRATIONS.keys()) == expected

    def test_each_has_required_fields(self):
        for itype, meta in api.SUPPORTED_INTEGRATIONS.items():
            assert "name" in meta, f"{itype} missing name"
            assert "description" in meta, f"{itype} missing description"
            assert "fields" in meta, f"{itype} missing fields"
            assert "events" in meta, f"{itype} missing events"
            assert isinstance(meta["fields"], list)
            assert isinstance(meta["events"], list)

    def test_field_keys_present(self):
        for itype, meta in api.SUPPORTED_INTEGRATIONS.items():
            for field in meta["fields"]:
                assert "key" in field, f"{itype} field missing key"
                assert "label" in field, f"{itype} field missing label"
                assert "type" in field, f"{itype} field missing type"

    def test_slack_webhook_field(self):
        slack = api.SUPPORTED_INTEGRATIONS["slack"]
        field_keys = [f["key"] for f in slack["fields"]]
        assert "webhook_url" in field_keys

    def test_webhook_has_secret_field(self):
        webhook = api.SUPPORTED_INTEGRATIONS["webhook"]
        field_keys = [f["key"] for f in webhook["fields"]]
        assert "secret" in field_keys
        assert "url" in field_keys


# ────────────────────────────────────────────────────────────────────────────
# auth_middleware
# ────────────────────────────────────────────────────────────────────────────

class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_healthz_skips_auth(self):
        request = MagicMock()
        request.path = "/healthz"
        handler = AsyncMock(return_value="ok")
        result = await api.auth_middleware(request, handler)
        handler.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_internal_skips_auth(self):
        request = MagicMock()
        request.path = "/internal/keys/anonymous/openai"
        handler = AsyncMock(return_value="ok")
        result = await api.auth_middleware(request, handler)
        handler.assert_called_once_with(request)

    @pytest.mark.asyncio
    async def test_dev_mode_sets_anonymous(self):
        """When AUTH0_DOMAIN is empty, unauthenticated access is allowed."""
        request = MagicMock()
        request.path = "/api/briefings"
        request.headers = {}
        handler = AsyncMock(return_value="ok")
        with patch.object(api, "AUTH0_DOMAIN", ""), patch.object(api, "AUTH0_AUDIENCE", ""):
            result = await api.auth_middleware(request, handler)
        assert request._user_id == "anonymous"
        handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_bearer_returns_401(self):
        request = MagicMock()
        request.path = "/api/briefings"
        request.headers = {"Authorization": "Basic abc"}
        handler = AsyncMock()
        with patch.object(api, "AUTH0_DOMAIN", "test.auth0.com"), \
             patch.object(api, "AUTH0_AUDIENCE", "https://api"):
            result = await api.auth_middleware(request, handler)
        assert result.status == 401
        handler.assert_not_called()
