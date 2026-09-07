"""Отказ owner-policy нельзя превращать в разрешение STT fallback."""

import pytest

from backend.ipc_throttle import IPCThrottle
import test_call_stt_backend_wiring as wiring

real_call_stack = wiring.real_call_stack


@pytest.mark.parametrize("owner_private", [False, True])
def test_owner_rate_limit_is_terminal_with_split_privacy_stores(real_call_stack, owner_private):
    stack = real_call_stack(owner_private=owner_private)
    # REST и IPC могут иметь разные DATA_DIR. Решение owner-policy не теряется,
    # даже когда его privacy guard ещё не был вызван из-за общего IPC throttle.
    stack.owner._ipc_throttle = IPCThrottle(limits={"light": 1})
    assert stack.owner._ipc_throttle.check_rate("transcribe_ephemeral_call")

    response = stack.post()

    assert response.status_code == 403
    assert response.json["text"] == ""
    assert len(stack.frames) == 1
    assert not stack.commands
    stack.transcriber.transcribe.assert_not_called()
    stack.rest_deps.transcriber.transcribe.assert_not_called()
    stack.store.add_history_item.assert_not_called()
    stack.rest_deps.store.add_history_item.assert_not_called()
