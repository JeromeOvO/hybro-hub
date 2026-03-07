"""Tests for hub.privacy_router."""

import pytest

from hub.privacy_router import PrivacyRouter, SensitivityLevel


class TestClassify:
    def test_low_by_default(self):
        router = PrivacyRouter()
        assert router.classify("What is the weather today?") == SensitivityLevel.LOW

    def test_keyword_match(self):
        router = PrivacyRouter(sensitive_keywords=["medical", "financial"])
        assert router.classify("Show me my medical records") == SensitivityLevel.HIGH

    def test_keyword_case_insensitive(self):
        router = PrivacyRouter(sensitive_keywords=["password"])
        assert router.classify("My PASSWORD is secret") == SensitivityLevel.HIGH

    def test_email_detection(self):
        router = PrivacyRouter()
        assert router.classify("Send to john@example.com") == SensitivityLevel.HIGH

    def test_phone_detection(self):
        router = PrivacyRouter()
        assert router.classify("Call me at 555-123-4567") == SensitivityLevel.HIGH

    def test_ssn_detection(self):
        router = PrivacyRouter()
        assert router.classify("SSN is 123-45-6789") == SensitivityLevel.HIGH

    def test_credit_card_detection(self):
        router = PrivacyRouter()
        assert router.classify("Card: 4111111111111111") == SensitivityLevel.HIGH

    def test_api_key_detection(self):
        router = PrivacyRouter()
        assert router.classify("Use sk_live_abc1234567890123456789") == SensitivityLevel.HIGH

    def test_user_regex_pattern(self):
        router = PrivacyRouter(sensitive_patterns=[r"PROJ-\d{4}"])
        assert router.classify("Reference PROJ-4521 for details") == SensitivityLevel.HIGH
        assert router.classify("No project reference here") == SensitivityLevel.LOW

    def test_no_false_positive_on_normal_text(self):
        router = PrivacyRouter(sensitive_keywords=["secret"])
        assert router.classify("The recipe is simple") == SensitivityLevel.LOW


class TestCheckAndLog:
    def test_returns_level(self):
        router = PrivacyRouter()
        level = router.check_and_log("Hello", agent_name="test")
        assert level == SensitivityLevel.LOW

    def test_high_logged(self):
        router = PrivacyRouter(sensitive_keywords=["password"])
        level = router.check_and_log("my password is 1234", agent_name="test")
        assert level == SensitivityLevel.HIGH
