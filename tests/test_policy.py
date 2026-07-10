from dataclasses import FrozenInstanceError

import pytest

from telegram_presence.policy import OwnerPrivateChatPolicy


def test_private_owner_policy_uses_only_immutable_numeric_id():
    policy = OwnerPrivateChatPolicy(owner_user_id=123456)
    assert policy.allows_private(123456) is True
    assert policy.allows_private(654321) is False
    assert policy.allows_private("123456") is False
    assert policy.allows_private(123456, chat_type="group") is False
    with pytest.raises(FrozenInstanceError):
        policy.owner_user_id = 654321


def test_private_owner_policy_rejects_username_style_configuration():
    with pytest.raises(ValueError, match="numeric"):
        OwnerPrivateChatPolicy(owner_user_id="owner")
    with pytest.raises(PermissionError):
        OwnerPrivateChatPolicy(123).require_private(456)
