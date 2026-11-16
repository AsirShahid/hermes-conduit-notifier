"""A script's message can go to a topic chat instead of the Bot Chat (`hermes conduit-push say --topic`).

Asir, 2026-11-16: he does not read the Bot Chat, so a note a script posts there is a note nobody sees.
"""
from test_cron_bot_chat import _topics, cron_bot_chat, hermes, pushes  # noqa: F401  (fixtures)


def _quiet(hermes, monkeypatch):
    hermes.owned = False  # `hermes conduit-push say` runs outside the chat's owner
    monkeypatch.setattr(cron_bot_chat.time, "sleep", lambda seconds: None)


def test_a_script_says_something_into_a_topic_chat_and_its_push_opens_that_chat(hermes, pushes, monkeypatch):
    _quiet(hermes, monkeypatch)
    _topics(hermes.home, {}, {"Server & maintenance": "server-chat"})
    hermes.sessions["server-chat"] = {"id": "server-chat", "archived": 0}
    assert cron_bot_chat.say("Pulse memory is off.", "Memory watch", topic="Server & maintenance") is None
    (chat, _key, text, kind, _metadata), = hermes.written
    assert chat == "server-chat" and kind == "internal_notification" and text.endswith("Pulse memory is off.")
    (event,) = pushes.events
    assert event["session_id"] == "server-chat" and event["title"] == "Memory watch"


def test_a_topic_that_is_unknown_gone_or_archived_keeps_the_bot_chat(hermes, pushes, monkeypatch):
    _quiet(hermes, monkeypatch)
    _topics(hermes.home, {}, {"Travel": "old-chat", "Sports": "gone-chat"})
    hermes.sessions["old-chat"] = {"id": "old-chat", "archived": 1}
    for topic in ("Travel", "Sports", "No such topic", "  "):
        assert cron_bot_chat.say("Rain at 9.", "Weather", topic=topic) is None
    assert [written[0] for written in hermes.written] == ["bot-chat-tip"] * 4
    assert [event["session_id"] for event in pushes.events] == ["bot-chat-tip"] * 4


def test_a_note_with_no_topic_and_one_with_an_open_link_go_where_they_always_did(hermes, pushes, monkeypatch):
    _quiet(hermes, monkeypatch)
    _topics(hermes.home, {}, {"Server & maintenance": "server-chat"})
    hermes.sessions["server-chat"] = {"id": "server-chat", "archived": 0}
    assert cron_bot_chat.say("Not logged yet today.", "Telmisartan", open_link="conduit://meds") is None
    assert hermes.written[0][0] == "bot-chat-tip" and pushes.events[0]["open"] == "conduit://meds"
