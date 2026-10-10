"""
The fleet's chat stream merges the answer's text (U72).

A model streams in small pieces; one Redis entry each would put thousands of
entries on a stream capped at 5,000 (approximate), and a reader that reattaches
replays every one. `ChatEventPublisher` holds `answer_delta`s for a short window
and sends them as one, and any other event sends what is held first.
"""
import asyncio
import json

from core.scale.pubsub import ChatEventPublisher


class _Redis:
    def __init__(self):
        self.entries = []

    async def xadd(self, key, fields, maxlen=None, approximate=None):
        self.entries.append(json.loads(fields["data"]))

    async def expire(self, key, ttl):
        pass


async def test_deltas_in_one_window_are_one_entry():
    redis = _Redis()
    pub = ChatEventPublisher(redis, "s1")
    for piece in ("Par", "is ", "is ", "big."):
        await pub.publish({"type": "answer_delta", "text": piece, "turn": 1})
    await asyncio.sleep(pub.DELTA_WINDOW * 2)
    assert redis.entries == [{"type": "answer_delta", "text": "Paris is big.", "turn": 1}]


async def test_another_event_sends_what_is_held_first():
    redis = _Redis()
    pub = ChatEventPublisher(redis, "s2")
    await pub.publish({"type": "answer_delta", "text": "Let me look.", "turn": 1})
    await pub.publish({"type": "answer_reset", "turn": 1, "reason": "tool_call"})
    await pub.publish({"type": "tool_execution", "tool_name": "x", "args": {}})
    await pub.publish_done()
    assert [e["type"] for e in redis.entries] == ["answer_delta", "answer_reset", "tool_execution", "done"]


async def test_a_new_turn_is_not_merged_into_the_last():
    redis = _Redis()
    pub = ChatEventPublisher(redis, "s3")
    await pub.publish({"type": "answer_delta", "text": "a", "turn": 1})
    await pub.publish({"type": "answer_delta", "text": "b", "turn": 2})
    await pub.publish_done()
    assert [(e.get("turn"), e.get("text")) for e in redis.entries[:2]] == [(1, "a"), (2, "b")]


async def test_other_events_are_sent_as_they_come():
    redis = _Redis()
    pub = ChatEventPublisher(redis, "s4")
    await pub.publish({"type": "status", "message": "x"})
    assert redis.entries == [{"type": "status", "message": "x"}]
