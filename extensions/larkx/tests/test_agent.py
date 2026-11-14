"""/agent: the user's own message goes to the hub as a channel message, and the answer comes back under it.
Run them: python3 -m unittest discover -s tests, in extensions/larkx."""
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_connector import ME, Backend, Hub, message  # noqa: E402

from larkx_persona import agent  # noqa: E402
from larkx_persona.connector import AGENT_SUFFIX, Connector, agent_request  # noqa: E402
from larkx_persona.main import Plugin  # noqa: E402
from larkx_persona.store import Store  # noqa: E402
from larkx_persona.wire import RpcError  # noqa: E402

# Synthetic Feishu user ids: digits, like the real ones.
ZHANG = "7000000000000000001"
LI = "7000000000000000002"
WANG = "7000000000000000003"


class ChannelHub(Hub):
    def __init__(self):
        super().__init__()
        self.received = []
        self.logged = []
        self.got = threading.Event()

    def call(self, method, params):
        if method == "channel.receive":
            self.received.append(params)
            self.got.set()
            return {}
        return super().call(method, params)

    def log(self, line):
        self.logged.append(line)


def me_at(text="@我"):
    return {"id": ME, "text": text}


class SettingsTest(unittest.TestCase):
    def test_the_defaults(self):
        s = agent.Settings({})
        self.assertTrue(s.trigger_prefix)
        self.assertEqual(s.prefix, "/agent")
        self.assertTrue(s.self_mention)
        self.assertTrue(s.owner_only)
        self.assertEqual((s.allow, s.deny, s.deny_reply), ({}, {}, ""))
        self.assertEqual((s.follow, s.follow_seconds), ("dm", 1800))

    def test_values_the_hub_would_not_pass_fall_back(self):
        s = agent.Settings({"agent_prefix": "  ", "agent_follow": "sometimes", "agent_follow_minutes": "5", "agent_owner_only": "no"})
        self.assertEqual((s.prefix, s.follow, s.follow_seconds, s.owner_only), ("/agent", "dm", 1800, True))
        self.assertEqual(agent.Settings({"agent_follow_minutes": 99999}).follow_seconds, 1440 * 60)

    def test_lists_are_kept_by_user_id_with_the_name_to_read(self):
        found = agent.people(["张三 " + ZHANG, "李四（%s）" % LI, WANG, "只有名字", 42, ""])
        self.assertEqual(found, {ZHANG: "张三", LI: "李四", WANG: ""})

    def test_who_may_ask(self):
        only_me = agent.Settings({"agent_allow": ["张三 " + ZHANG, "李四 " + LI], "agent_deny": ["李四 " + LI]})
        self.assertTrue(only_me.may_ask(ME, ME))
        self.assertTrue(only_me.may_ask(ZHANG, ME))
        # The deny list wins over the allow list.
        self.assertFalse(only_me.may_ask(LI, ME))
        self.assertFalse(only_me.may_ask(WANG, ME))
        self.assertFalse(only_me.may_ask("", ME))
        anyone = agent.Settings({"agent_owner_only": False, "agent_deny": [WANG], "agent_allow": [ZHANG]})
        self.assertTrue(anyone.may_ask(ZHANG, ME))
        self.assertTrue(anyone.may_ask(LI, ME))
        self.assertFalse(anyone.may_ask(WANG, ME))
        self.assertTrue(anyone.may_ask(ME, ME))
        # The user may always ask, even listed as denied.
        self.assertTrue(agent.Settings({"agent_deny": [ME]}).may_ask(ME, ME))

    def test_which_topics_are_followed(self):
        self.assertEqual([agent.Settings({"agent_follow": f}).follows(k) for f in ("dm", "all", "off") for k in ("dm", "group")],
                         [True, False, True, True, False, False])


class ReadTest(unittest.TestCase):
    def read(self, text, sender=ME, mentions=None, msg_type="TEXT", **config):
        return agent.read({"text": text, "sender": sender, "mentions": mentions or [], "type": msg_type}, ME, agent.Settings(config))

    def test_a_prefix_of_ones_own(self):
        self.assertEqual(self.read("/ai 总结", agent_prefix="/ai"), ("总结", "prefix"))
        self.assertEqual(self.read("/AI：总结", agent_prefix="/ai"), ("总结", "prefix"))
        self.assertEqual(self.read("/ai", agent_prefix="/ai"), ("", "prefix"))
        self.assertIsNone(self.read("/ais 总结", agent_prefix="/ai"))
        # The old prefix no longer counts.
        self.assertIsNone(self.read("/agent 总结", agent_prefix="/ai"))
        self.assertEqual(self.read("小助手 帮个忙", agent_prefix="小助手"), ("帮个忙", "prefix"))

    def test_the_prefix_after_mentions_at_the_start(self):
        mentions = [{"id": ZHANG, "text": "@张三 Smith"}, {"id": LI, "text": "@李四"}]
        self.assertEqual(self.read("@张三 Smith @李四 /agent 问一下", mentions=mentions), ("问一下", "prefix"))
        self.assertIsNone(self.read("你好 /agent 在中间"))

    def test_the_prefix_can_be_off(self):
        self.assertIsNone(self.read("/agent 总结", agent_trigger_prefix=False))
        self.assertEqual(self.read("@我 总结", mentions=[me_at()], agent_trigger_prefix=False), ("总结", "mention"))

    def test_a_self_mention_by_user_id(self):
        self.assertEqual(self.read("@我 帮我总结一下", mentions=[me_at()]), ("帮我总结一下", "mention"))
        self.assertEqual(self.read("这个 @我 记一下", mentions=[me_at()]), ("这个  记一下", "mention"))
        self.assertEqual(self.read("@我", mentions=[me_at()]), ("", "mention"))
        # A name that reads like the user's is no proof: only the user's id is.
        self.assertIsNone(self.read("@我 帮我总结一下", mentions=[{"id": ZHANG, "text": "@我"}]))
        self.assertIsNone(self.read("@我 帮我总结一下"))
        # Someone else @-mentioning the user is not the user's self-mention.
        self.assertIsNone(self.read("@我 帮我总结一下", sender=ZHANG, mentions=[me_at()]))
        self.assertIsNone(self.read("@我 总结", mentions=[me_at()], agent_trigger_self_mention=False))

    def test_the_prefix_and_a_self_mention_count_once(self):
        self.assertEqual(self.read("@我 /agent 总结", mentions=[me_at()]), ("总结", "prefix"))
        self.assertEqual(self.read("/agent @我 总结", mentions=[me_at()]), ("总结", "prefix"))

    def test_a_post(self):
        self.assertEqual(self.read("[富文本] \n/agent 看下这张图[图片]", msg_type="POST"), ("看下这张图[图片]", "prefix"))
        self.assertEqual(self.read("[富文本] /agent 标题里\n正文", msg_type="POST"), ("标题里\n正文", "prefix"))

    def test_stop(self):
        for text in ("/stop", " /STOP ", "/停止", "/停", "@我 /stop"):
            self.assertTrue(agent.is_stop(text, [me_at()]), text)
        for text in ("/stop 一下", "/stopped", "停"):
            self.assertFalse(agent.is_stop(text, [me_at()]), text)


class AgentRequestTest(unittest.TestCase):
    def test_what_counts_as_a_request(self):
        self.assertEqual(agent_request("/agent 总结一下"), "总结一下")
        self.assertEqual(agent_request("  /Agent：帮我看看"), "帮我看看")
        self.assertEqual(agent_request("/agent\n第二行"), "第二行")
        self.assertEqual(agent_request("/agent"), "")
        self.assertIsNone(agent_request("/agents 列表"))
        self.assertIsNone(agent_request("你好 /agent 在中间"))
        self.assertIsNone(agent_request(""))


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.connect()

    def connect(self, **config):
        """A signed-in connector with these agent_* settings, on the same store as before."""
        self.backend = Backend()
        self.hub = ChannelHub()
        self.c = Connector(self.backend, Store(os.path.join(self.dir.name, "x.db")), self.hub, "", agent.Settings(config))
        self.c._ready.set()
        self.backend.poll = "done"
        self.assertTrue(self.c.login_step({"step": "scan"})["done"])
        self.addCleanup(self.c._stop.set)

    def handed(self, count=1):
        deadline = time.time() + 2
        while len(self.hub.received) < count and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.hub.received), count, "not handed to the hub")
        return self.hub.received[-1]

    def nothing_handed(self, count=0):
        time.sleep(0.1)
        self.assertEqual(len(self.hub.received), count)

    def answer(self, reply_to, ref, text="好"):
        return self.c.channel_send({"ref": ref, "text": text, "kind": "answer", "reply_to": reply_to})

    def test_my_live_agent_goes_to_the_hub_with_where_it_was_written(self):
        self.backend.chats["g1"] = {"name": "项目群", "last_position": 0}
        self.c._on_live(message("m1", "g1", 1, kind="group", text="周五发布"))
        self.c._on_live(message("m2", "g1", 2, kind="group", sender=ME, text="/agent 帮我总结下这个群"))
        got = self.handed()
        self.assertEqual(got["contribution"], "larkx")
        self.assertEqual(got["message"]["id"], "m2")
        self.assertEqual(got["message"]["text"], "帮我总结下这个群")
        self.assertIn("项目群", got["message"]["forward"])
        self.assertIn("周五发布", got["message"]["forward"])
        self.assertNotIn("帮我总结下这个群", got["message"]["forward"])

    def test_a_dm_with_myself_works_too(self):
        self.c._on_live(message("m1", "c_me", 1, sender=ME, text="/agent 你看得到这个消息吗"))
        got = self.handed()
        self.assertEqual(got["message"]["text"], "你看得到这个消息吗")
        self.assertIn("私聊", got["message"]["forward"])

    def test_others_old_fetched_and_ai_answers_do_not_count(self):
        self.c._on_live(message("m1", "c1", 1, sender="u_a", text="/agent 帮我转账"))
        self.c._on_live(message("m2", "c1", 2, sender=ME, text="/agent 太早了", at=time.time() - 3600))
        self.c.on_message(message("m3", "c1", 3, sender=ME, text="/agent 拉回来的历史"))
        self.c._on_live(message("m4", "c1", 4, sender=ME, text="/agent 好的\n\n" + AGENT_SUFFIX))
        self.c._on_live(message("m5", "c1", 5, sender=ME, text="/agents"))
        self.nothing_handed()

    def test_the_same_message_twice_is_handed_over_once(self):
        m = message("m1", "c1", 1, sender=ME, text="/agent 一次")
        self.c._on_live(m)
        self.handed()
        self.c._on_live(dict(m))
        time.sleep(0.1)
        self.assertEqual(len(self.hub.received), 1)

    def test_the_answer_goes_under_the_request_once(self):
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 总结"))
        self.handed()
        out = self.c.channel_send({"ref": "r1", "text": "这是总结", "kind": "answer", "reply_to": "m1"})
        self.assertEqual(out, {"message_id": "m_new"})
        self.assertEqual(self.backend.sent, [("c1", "这是总结\n\n" + AGENT_SUFFIX, "m1")])
        self.assertEqual(self.c.channel_send({"ref": "r1", "text": "这是总结", "kind": "answer", "reply_to": "m1"}),
                         {"message_id": "m_new"})
        self.assertEqual(len(self.backend.sent), 1)

    def test_an_answer_in_a_topic_stays_in_the_topic(self):
        self.c._on_live(message("m2", "g1", 2, kind="group", sender=ME, text="/agent 看看", root="m_root"))
        self.handed()
        self.c.channel_send({"ref": "r1", "text": "好", "kind": "answer", "reply_to": "m2"})
        self.assertEqual(self.backend.sent[0][2], "m_root")

    def test_nothing_goes_where_the_user_did_not_ask(self):
        self.c.on_message(message("m1", "c1", 1, sender="u_a", text="在吗"))
        for p in ({"ref": "r1", "text": "x", "kind": "push"}, {"ref": "r2", "text": "x", "kind": "answer", "reply_to": "m1"},
                  {"ref": "r3", "text": "x", "kind": "answer", "reply_to": "m_unknown"}):
            with self.assertRaises(RpcError) as e:
                self.c.channel_send(p)
            self.assertEqual(e.exception.code, "cannot_send")
        with self.assertRaises(RpcError):
            self.c.channel_send({"text": "x", "kind": "answer", "reply_to": "m1"})
        self.assertEqual(self.backend.sent, [])

    def test_an_answer_only_goes_to_an_agent_request(self):
        # A normal message from the user is not an /agent, and our own answer is not one either.
        self.c.on_message(message("m_plain", "c1", 1, sender=ME, text="ordinary message"))
        self.c.on_message(message("m_ai", "c1", 2, sender=ME, text="答\n\n" + AGENT_SUFFIX))
        for mid in ("m_plain", "m_ai"):
            with self.assertRaises(RpcError) as e:
                self.c.channel_send({"ref": "r_" + mid, "text": "x", "kind": "answer", "reply_to": mid})
            self.assertEqual(e.exception.code, "cannot_send")
        self.assertEqual(self.backend.sent, [])

    def test_an_unknown_result_is_never_sent_again(self):
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 发"))
        self.handed()
        self.backend.send_error = OSError("cut off")
        with self.assertRaises(RpcError):
            self.c.channel_send({"ref": "r1", "text": "x", "kind": "answer", "reply_to": "m1"})
        self.backend.send_error = None
        with self.assertRaises(RpcError):
            self.c.channel_send({"ref": "r1", "text": "x", "kind": "answer", "reply_to": "m1"})
        self.assertEqual(self.backend.sent, [])

    # the prefix and the self-mention

    def test_a_prefix_of_ones_own_replaces_the_old_one(self):
        self.connect(agent_prefix="/x")
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 旧前缀"))
        self.nothing_handed()
        self.c._on_live(message("m2", "c1", 2, sender=ME, text="/X 新前缀"))
        self.assertEqual(self.handed()["message"]["text"], "新前缀")
        # Only the prefix: the request says so with the prefix in force.
        self.c._on_live(message("m3", "c1", 3, sender=ME, text="/x"))
        self.assertIn("/x", self.handed(2)["message"]["text"])
        # An /agent kept from before the change is no request to answer any more.
        with self.assertRaises(RpcError) as e:
            self.answer("m1", "r1")
        self.assertEqual(e.exception.code, "cannot_send")
        self.assertEqual(self.answer("m2", "r2"), {"message_id": "m_new"})

    def test_a_self_mention_is_a_request(self):
        self.c._on_live(message("m1", "g1", 1, kind="group", sender=ME, text="@我 帮我记下这件事", mentions=[me_at()]))
        self.assertEqual(self.handed()["message"]["text"], "帮我记下这件事")
        self.c._on_live(message("m2", "g1", 2, kind="group", sender=ME, text="@我 /agent 一次就好", mentions=[me_at()]))
        self.assertEqual(self.handed(2)["message"]["text"], "一次就好")
        self.c._on_live(message("m3", "g1", 3, kind="group", sender=ME, text="@我", mentions=[me_at()]))
        self.assertIn("@ 了自己", self.handed(3)["message"]["text"])
        self.assertEqual(self.answer("m1", "r1"), {"message_id": "m_new"})

    def test_the_triggers_can_be_turned_off(self):
        self.connect(agent_trigger_prefix=False, agent_trigger_self_mention=False)
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 总结"))
        self.c._on_live(message("m2", "c1", 2, sender=ME, text="@我 总结", mentions=[me_at()]))
        self.nothing_handed()

    # someone else

    def test_nobody_else_reaches_the_assistant(self):
        """Allowed or not, someone else's request is never handed over: the hub's channel has no guest conversation."""
        self.connect(agent_allow=["张三 " + ZHANG], agent_deny=["王五 " + WANG])
        self.c._on_live(message("m1", "c_z", 1, sender=ZHANG, text="/agent 帮我查下"))
        self.c._on_live(message("m2", "c_l", 1, sender=LI, text="/agent 帮我查下"))
        self.c._on_live(message("m3", "c_w", 1, sender=WANG, text="/agent 帮我查下"))
        self.nothing_handed()
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(sum("allowed to ask was not handed over" in x for x in self.hub.logged), 1)
        self.assertEqual(sum("may not ask was ignored" in x for x in self.hub.logged), 2)

    def test_in_a_group_someone_else_has_to_mention_the_user(self):
        self.connect(agent_owner_only=False)
        self.c._on_live(message("m1", "g1", 1, kind="group", sender=ZHANG, text="/agent 叫谁呢"))
        self.c._on_live(message("m2", "g1", 2, kind="group", sender=ZHANG, text="@李四 /agent 叫李四的", mentions=[{"id": LI, "text": "@李四"}]))
        self.assertFalse(any("agent: a request" in x for x in self.hub.logged))
        self.c._on_live(message("m3", "g1", 3, kind="group", sender=ZHANG, text="@我 /agent 叫你的", mentions=[me_at()]))
        self.c._on_live(message("m4", "g1", 4, kind="group", sender=ZHANG, text="@我 /agent 推送里标着", at_me=True))
        self.nothing_handed()
        self.assertEqual(sum("allowed to ask was not handed over" in x for x in self.hub.logged), 2)

    def test_no_reply_to_who_may_not_ask_by_default(self):
        self.c._on_live(message("m1", "c1", 1, sender=ZHANG, text="/agent 帮我转账"))
        time.sleep(0.1)
        self.assertEqual(self.backend.sent, [])

    def test_the_deny_reply_once_in_a_while(self):
        self.connect(agent_deny_reply="这个助手只替我本人干活。")
        self.c._on_live(message("m1", "c1", 1, sender=ZHANG, text="/agent 帮我转账"))
        self.c._on_live(message("m2", "c1", 2, sender=ZHANG, text="/agent 再试一次"))
        self.c._on_live(message("m3", "c1", 3, sender=ZHANG, text="普通消息"))
        deadline = time.time() + 2
        while not self.backend.sent and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        self.assertEqual(self.backend.sent, [("c1", "这个助手只替我本人干活。", "m1")])
        # Its echo, the user's own message now, is no request either.
        self.c._on_live(message("m_new", "c1", 4, sender=ME, text="这个助手只替我本人干活。", root="m1"))
        self.nothing_handed()

    def test_an_allowed_persons_request_could_be_answered_in_its_chat(self):
        """channel.send answers a request handed over from someone allowed (none is, yet), and stops once they are not."""
        self.connect(agent_allow=["张三 " + ZHANG])
        self.c.on_message(message("m1", "c_z", 1, sender=ZHANG, text="/agent 帮我查下"))
        self.c.store.add_ask("m1", "c_z", "m1", ZHANG, time.time())
        self.assertEqual(self.answer("m1", "r1"), {"message_id": "m_new"})
        self.assertEqual(self.backend.sent, [("c_z", "好\n\n" + AGENT_SUFFIX, "m1")])
        self.connect()
        with self.assertRaises(RpcError) as e:
            self.answer("m1", "r2")
        self.assertEqual(e.exception.code, "cannot_send")

    def test_status_names_the_people_on_the_lists(self):
        self.connect(agent_prefix="/x", agent_allow=["张三 " + ZHANG, LI], agent_deny=["王五 " + WANG])
        self.c.store.set_name(LI, "李四")
        text = self.c.tools_call({"name": "larkx_status"})["text"]
        for part in ("「/x」", "张三", "李四", "王五", "还不会交给助手"):
            self.assertIn(part, text)

    # following a topic

    def test_a_dm_topic_goes_on_without_the_prefix(self):
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 第一个问题"))
        self.handed()
        self.answer("m1", "r1")
        self.c._on_live(message("m2", "c1", -3, sender=ME, text="接着问", root="m1"))
        got = self.handed(2)["message"]
        self.assertEqual(got["text"], "接着问")
        self.assertIn("接着说的", got["forward"])
        self.assertEqual(self.answer("m2", "r2"), {"message_id": "m_new"})
        self.assertEqual(self.backend.sent[-1][2], "m1")
        # Outside the topic a message without the prefix is just a message.
        self.c._on_live(message("m3", "c1", 2, sender=ME, text="主线上的话"))
        self.nothing_handed(2)

    def test_stop_ends_the_topic(self):
        for word in ("/stop", "/停止"):
            self.c._on_live(message("q_" + word, "c1", 1, sender=ME, text="/agent 问"))
            self.c._on_live(message("s_" + word, "c1", -3, sender=ME, text=word, root="q_" + word))
            self.c._on_live(message("t_" + word, "c1", -3, sender=ME, text="之后的话", root="q_" + word))
        self.handed(4)
        self.nothing_handed(4)
        handed = sorted((r["message"]["id"], r["message"]["text"]) for r in self.hub.received)
        self.assertEqual(handed, [("q_/stop", "问"), ("q_/停止", "问"), ("s_/stop", "/stop"), ("s_/停止", "/stop")])
        # The hub's "stopped" note goes under the /stop.
        self.assertEqual(self.c.channel_send({"ref": "n1", "text": "已停止", "kind": "note", "reply_to": "s_/stop"}), {"message_id": "m_new"})

    def test_a_topic_goes_quiet(self):
        self.connect(agent_follow_minutes=1)
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 问"))
        self.handed()
        self.c.store._do("UPDATE agent_topics SET last_at = ?", time.time() - 61)
        self.c._on_live(message("m2", "c1", -3, sender=ME, text="过了一会儿", root="m1"))
        self.c._on_live(message("m3", "c1", -3, sender=ME, text="/stop", root="m1"))
        self.nothing_handed(1)
        self.assertIsNone(self.c.store.topic("m1"))

    def test_group_topics_are_followed_only_when_asked_for(self):
        self.c._on_live(message("m1", "g1", 1, kind="group", sender=ME, text="/agent 问"))
        self.handed()
        self.c._on_live(message("m2", "g1", -3, kind="group", sender=ME, text="跟同事说的话", root="m1"))
        self.nothing_handed(1)
        # /stop still stops what was asked in a group topic.
        self.c._on_live(message("m3", "g1", -3, kind="group", sender=ME, text="/停", root="m1"))
        self.assertEqual(self.handed(2)["message"]["text"], "/stop")
        self.connect(agent_follow="all")
        self.c._on_live(message("m4", "g1", 2, kind="group", sender=ME, text="/agent 再问"))
        self.c._on_live(message("m5", "g1", -3, kind="group", sender=ME, text="接着问", root="m4"))
        self.assertEqual(self.handed(2)["message"]["text"], "接着问")

    def test_following_can_be_off_and_others_are_never_followed(self):
        self.connect(agent_follow="off")
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 问"))
        self.handed()
        self.c._on_live(message("m2", "c1", -3, sender=ME, text="接着问", root="m1"))
        self.nothing_handed(1)
        self.connect(agent_owner_only=False, agent_follow="all")
        self.c._on_live(message("m3", "c1", -3, sender=ZHANG, text="我也说一句", root="m1"))
        self.nothing_handed()

    def test_our_own_sends_in_a_followed_topic_are_no_requests(self):
        self.c._on_live(message("m1", "c1", 1, sender=ME, text="/agent 问"))
        self.handed()
        self.backend.send_result = "m_sent"
        self.c.actions_do({"name": "larkx_send", "request_id": "q1", "args": {"chat": "c1", "text": "确认后发的", "reply_to": "m1"}})
        self.c._on_live(message("m_sent", "c1", -3, sender=ME, text="确认后发的", root="m1"))
        self.nothing_handed(1)

    # context

    def test_in_a_topic_the_context_is_the_topic(self):
        now = time.time()
        self.c.on_message(message("m9", "c1", 9, sender=ME, text="主线上更早的话", at=now - 50))
        self.c.on_message(message("r1", "c1", 10, sender=ME, text="话题开头", at=now - 40))
        self.c.on_message(message("x1", "c1", -3, sender="u_a", text="话题里的回复", root="r1", at=now - 30))
        self.c.on_message(message("m11", "c1", 11, sender=ME, text="主线上后来的话", at=now - 25))
        self.c._on_live(message("q1", "c1", -3, sender=ME, text="/agent 总结这个话题", root="r1", at=now - 20))
        forward = self.handed()["message"]["forward"]
        self.assertIn("话题", forward)
        self.assertIn("话题开头", forward)
        self.assertIn("话题里的回复", forward)
        self.assertNotIn("主线上", forward)
        self.assertNotIn("总结这个话题", forward)
        self.assertEqual(self.backend.topic_calls, [])

    def test_outside_a_topic_the_context_leaves_topic_replies_out(self):
        now = time.time()
        self.c.on_message(message("m1", "c1", 1, text="主线一", at=now - 50))
        self.c.on_message(message("x1", "c1", -3, text="话题里的回复", root="m1", at=now - 40))
        self.c.on_message(message("m2", "c1", 2, text="主线二", at=now - 30))
        self.c._on_live(message("q1", "c1", 3, sender=ME, text="/agent 总结", at=now - 20))
        forward = self.handed()["message"]["forward"]
        self.assertIn("主线一", forward)
        self.assertIn("主线二", forward)
        self.assertNotIn("话题里的回复", forward)

    def test_a_topic_begun_before_is_read_from_feishu(self):
        now = time.time()
        self.backend.topics["r_old"] = [message("r_old", "c1", 5, text="很早的话题开头", at=now - 86400),
                                        message("x_old", "c1", -3, text="很早的回复", root="r_old", at=now - 86000)]
        self.c._on_live(message("q1", "c1", -3, sender=ME, text="/agent 这个话题说了啥", root="r_old", at=now - 5))
        forward = self.handed()["message"]["forward"]
        self.assertIn("很早的话题开头", forward)
        self.assertIn("很早的回复", forward)
        self.assertEqual(self.backend.topic_calls, [("c1", "r_old")])

    def test_a_topic_that_cannot_be_read_still_goes_over(self):
        self.backend.thread = None  # calling it raises TypeError
        self.c._on_live(message("q1", "c1", -3, sender=ME, text="/agent 问", root="r_gone"))
        self.assertIn("话题", self.handed()["message"]["forward"])
        self.assertTrue(any("reading the topic failed" in x for x in self.hub.logged))


class PluginRoutingTest(unittest.TestCase):
    def test_channel_methods_reach_the_connector(self):
        plugin = Plugin({"id": "larkx", "version": "0", "contributes": {}}, lambda *_: None)

        class Fake:
            methods = {}
            channel_methods = {"typing": lambda p: {"typed": True}}

        plugin.connector = Fake()
        self.assertEqual(plugin.handle("channel.typing", {"contribution": "larkx", "on": True}), {"typed": True})
        with self.assertRaises(RpcError) as e:
            plugin.handle("channel.send", {"contribution": "other"})
        self.assertEqual(e.exception.code, "unknown_method")
        with self.assertRaises(RpcError) as e:
            plugin.handle("channel.typing", {"contribution": "other"})
        self.assertEqual(e.exception.code, "no_contribution")

    def test_the_settings_reach_the_connector(self):
        class Peer:
            def call(self, method, params):
                return {}

            def emit(self, event, params):
                pass

        with tempfile.TemporaryDirectory() as data, mock.patch("sys.stderr", new=io.StringIO()) as log:
            plugin = Plugin({"id": "larkx", "version": "0", "contributes": {}}, lambda *_: Backend())
            plugin.peer = Peer()
            plugin.initialize({"protocol": 1, "extension": {"data_dir": data},
                               "config": {"agent_prefix": "/x", "agent_allow": ["张三 " + ZHANG], "agent_follow": "all"}})
            self.addCleanup(plugin.connector._stop.set)
            self.assertEqual(plugin.connector.agent.prefix, "/x")
            self.assertEqual(plugin.connector.agent.allow, {ZHANG: "张三"})
            self.assertEqual(plugin.connector.agent.follow, "all")
            # The log has the rules in force, without the people's names or ids.
            self.assertIn("prefix /x", log.getvalue())
            self.assertNotIn(ZHANG, log.getvalue())
            self.assertNotIn("张三", log.getvalue())


if __name__ == "__main__":
    unittest.main()
