"""The backend on the real LarkAgentX, with no network and no account: a push in LarkAgentX's own wire format through its
decoders, and credentials restored from the hub's store. It needs what the program downloads on first start, from the
data directory of an installed extension or a run of its own:
  LARKX_UPSTREAM=<its larkagentx-… directory> LARKX_DEPS=<its deps-… directory> python3 tests/test_real.py"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

UPSTREAM = os.environ.get("LARKX_UPSTREAM", "")
DEPS = os.environ.get("LARKX_DEPS", "")


@unittest.skipUnless(UPSTREAM and DEPS, "needs LARKX_UPSTREAM and LARKX_DEPS")
class RealTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path[:0] = [UPSTREAM, DEPS]
        cls.home = tempfile.TemporaryDirectory()
        os.environ["LARKX_HOME"] = cls.home.name
        import larkx
        from larkx import media
        from larkx.proto import builders, decoders
        from larkx.proto import proto_pb2 as P
        cls.larkx, cls.media, cls.decoders, cls.builders, cls.P = larkx, media, decoders, builders, P

    @classmethod
    def tearDownClass(cls):
        cls.home.cleanup()

    def frame(self, messages, at_me=()):
        P = self.P
        push = P.PushMessagesRequest()
        for m in messages:
            push.messages[m.id].CopyFrom(m)
        for key in at_me:
            push.messagesAtMe[key] = True
        packet = P.Packet()
        packet.cmd = 6
        packet.payload = push.SerializeToString()
        frame = P.Frame()
        frame.payload = packet.SerializeToString()
        return frame.SerializeToString()

    def message(self, msg_id, msg_type, content, chat_type=2, root=""):
        m = self.P.entities.Message()
        m.id, m.fromId, m.chatId = msg_id, "7001", "6001"
        m.type = msg_type
        m.chatType = chat_type
        m.createTime = 1793000000
        m.position = 42
        m.content = content
        if root:
            m.rootId = root
        return m

    def text(self, value):
        tc = self.P.TextContent()
        tc.text = value
        return tc.SerializeToString()

    def normalized(self, raw):
        from larkx_persona.real import mentions, normalize
        read = lambda data: mentions(data, self.P.AtProperty)  # noqa: E731
        return [normalize(m, self.media.extract_resource_key, read) for m in self.decoders.decode_push_messages(raw)]

    def rich_text(self, parts):
        """A text message's content from ("text", words) and ("at", user id, "@name") parts, in order."""
        tc = self.P.TextContent()
        for i, part in enumerate(parts):
            key = str(i + 1)
            element = tc.richText.elements.dictionary[key]
            if part[0] == "at":
                prop = self.P.AtProperty()
                prop.userId, prop.content = part[1], part[2]
                element.tag = 5
            else:
                prop = self.P.TextProperty()
                prop.content = part[1]
                element.tag = 1
            element.property = prop.SerializeToString()
            tc.richText.elementIds.append(key)
        return tc.SerializeToString()

    def test_mentions_are_read_by_user_id(self):
        content = self.rich_text([("at", "7002", "@我"), ("text", " /agent 帮我看下 "), ("at", "7003", "@小李")])
        m = self.normalized(self.frame([self.message("9005", 4, content)]))[0]
        self.assertEqual(m["text"], "@我 /agent 帮我看下 @小李")
        self.assertEqual(m["mentions"], [{"id": "7002", "text": "@我"}, {"id": "7003", "text": "@小李"}])
        plain = self.normalized(self.frame([self.message("9006", 4, self.text("没有 @ 谁"))]))[0]
        self.assertEqual(plain["mentions"], [])

    def test_a_topic_is_read_from_the_gateway(self):
        from protobuf_to_dict import protobuf_to_dict
        from larkx_persona.real import RealBackend
        root = protobuf_to_dict(self.message("9100", 4, self.text("话题开头"), chat_type=1))
        reply = protobuf_to_dict(self.message("9101", 4, self.text("回复"), chat_type=1, root="9100"))
        asked = []

        class Client:
            def api(self, name, data):
                asked.append((name, data))
                if name == "thread.PullThreadsByIDsRequest":
                    return {"threads": {"9100": {"id": "9100", "lastMessagePosition": 3}}, "rootMessages": {"9100": root}}
                return {"messages": {"3": reply}}

        backend = RealBackend(ROOT, self.home.name, "feishu")
        backend._client, backend._decoders = Client(), self.decoders
        backend._extract_key = self.media.extract_resource_key
        backend._read_mentions = lambda data: []
        got = backend.thread("6001", "9100", 2)
        self.assertEqual([(m["id"], m["text"], m["root"], m["chat_kind"]) for m in got],
                         [("9100", "话题开头", "", "dm"), ("9101", "回复", "9100", "dm")])
        self.assertEqual(asked[1], ("thread.PullThreadMessagesByPositionsRequest", {"threadId": "9100", "positions": [2, 3]}))

    def test_a_text_in_a_group_that_mentions_the_user(self):
        m = self.normalized(self.frame([self.message("9001", 4, self.text("周五开会"))], at_me=["9001"]))[0]
        self.assertEqual(m["id"], "9001")
        self.assertEqual(m["chat"], "6001")
        self.assertEqual(m["chat_kind"], "group")
        self.assertEqual(m["sender"], "7001")
        self.assertEqual(m["position"], 42)
        self.assertEqual(m["type"], "TEXT")
        self.assertEqual(m["text"], "周五开会")
        self.assertEqual(m["at"], 1793000000.0)
        self.assertTrue(m["at_me"])

    def test_a_direct_message(self):
        m = self.normalized(self.frame([self.message("9002", 4, self.text("在吗"), chat_type=1)]))[0]
        self.assertEqual(m["chat_kind"], "dm")
        self.assertFalse(m["at_me"])

    def test_a_topic_reply_keeps_its_root(self):
        m = self.normalized(self.frame([self.message("9003", 4, self.text("好"), root="9000")]))[0]
        self.assertEqual(m["root"], "9000")

    def test_a_picture_has_its_key(self):
        content = self.P.ImageContent()
        content.imageV2.imageKey = "img_key"
        m = self.normalized(self.frame([self.message("9004", 5, content.SerializeToString())]))[0]
        self.assertEqual(m["type"], "IMAGE")
        self.assertEqual(m["media"], [{"kind": "image", "key": "img_key"}])

    # sending: what a reply's packet says (the server shows a reply under the message parentId names)

    def request(self, packet):
        """The PutMessageRequest a send packet carries."""
        self.assertEqual(packet.cmd, 5)
        req = self.P.PutMessageRequest()
        req.ParseFromString(packet.payload)
        return req

    def packet(self, **kwargs):
        from larkx_persona.real import message_packet
        return self.request(message_packet(self.builders, self.P, "收到", "6001", "req-1", **kwargs))

    def test_an_answer_to_a_request_that_starts_its_topic(self):
        """The request 9100 is in no topic (a plain message, or a quote reply to someone else's in the main chat): the
        answer opens a topic on it and quotes it, as LarkAgentX's own send_msg would."""
        req = self.packet(root="9100", parent="9100")
        self.assertEqual((req.chatId, req.rootId, req.parentId, req.isReplyInThread), ("6001", "9100", "9100", True))
        self.assertEqual(req.content.richText.innerText, "收到")

    def test_an_answer_to_a_request_in_someone_elses_topic(self):
        """The request 9105 was written in the topic of someone's 9100: the answer stays in 9100's topic and quotes 9105."""
        req = self.packet(root="9100", parent="9105")
        self.assertEqual((req.chatId, req.rootId, req.parentId, req.isReplyInThread), ("6001", "9100", "9105", True))
        self.assertEqual(req.content.richText.innerText, "收到")
        self.assertEqual(req.type, 4)

    def test_a_topic_group_gets_no_reply_in_thread(self):
        # Topic groups refuse it (LarkAgentX's builder says so); the quote is set all the same.
        req = self.packet(root="9100", parent="9105", thread_chat=True)
        self.assertEqual((req.rootId, req.parentId), ("9100", "9105"))
        self.assertFalse(req.HasField("isReplyInThread"))

    def test_without_a_topic_nothing_is_quoted(self):
        for parent in (None, "9105"):
            req = self.packet(root=None, parent=parent)
            self.assertFalse(req.HasField("rootId") or req.HasField("parentId") or req.HasField("isReplyInThread"))
        # A parent not given quotes the root, as LarkAgentX's own send does.
        self.assertEqual(self.packet(root="9100").parentId, "9100")

    def answer(self, msg_id="", status=0):
        """The gateway's answer to a send: the new message's id, or none and a status."""
        reply = self.P.Packet()
        reply.status = status
        response = self.P.PutMessageResponse()
        if msg_id:
            response.message.id = msg_id
        reply.payload = response.SerializeToString()
        return reply.SerializeToString()

    def test_an_answer_without_a_message_id_was_not_taken(self):
        from larkx_persona.real import NotSent, sent_id
        self.assertEqual(sent_id(self.answer("9200"), self.P), "9200")
        self.assertEqual(self.builders.decode_put_message_response(self.answer("9200")), "9200")
        with self.assertRaises(NotSent) as e:
            sent_id(self.answer(status=7), self.P)
        self.assertEqual(e.exception.why, "Feishu returned no message id (gateway status 7)")

    def test_send_posts_the_packet_through_larkagentx(self):
        from larkx_persona.real import NotSent, RealBackend
        P, posted, asked, answer = self.P, [], [], self.answer

        class Client:
            fail = None

            def is_thread_chat(self, chat):
                asked.append(chat)
                return False

            def _gateway_post(self, packet):
                if isinstance(self.fail, Exception):
                    raise self.fail
                posted.append(packet)
                return answer(status=3) if self.fail else answer("9200")

        backend = RealBackend(ROOT, self.home.name, "feishu")
        backend._client, backend._builders, backend._proto = Client(), self.builders, P
        backend._request_id = lambda: "req-1"
        self.assertEqual(backend.send("6001", "收到", "9100", "9105"), "9200")
        req = self.request(posted[0])
        self.assertEqual((req.rootId, req.parentId, req.isReplyInThread), ("9100", "9105", True))
        self.assertEqual(posted[0].cid, "req-1")
        self.assertEqual(asked, ["6001"])
        self.assertEqual(backend.send("6001", "收到", None), "9200")
        self.assertFalse(self.request(posted[1]).HasField("rootId"))
        self.assertEqual(asked, ["6001"])
        # A send that cannot be confirmed raises: the connector never tries it again.
        backend._client.fail = OSError("cut off")
        with self.assertRaises(OSError):
            backend.send("6001", "收到", "9100", "9105")
        backend._client.fail = "refused"
        with self.assertRaises(NotSent):
            backend.send("6001", "收到", "9100", "9105")

    def test_credentials_go_through_the_hub_and_never_a_file(self):
        from larkx_persona.real import RealBackend
        saved = []
        backend = RealBackend(ROOT, self.home.name, "feishu")
        backend._lark = self.larkx
        backend.on_credentials(saved.append)
        creds = {"region": "feishu", "cookies": {"session": "s"}, "device_id": "d", "app_key": "a", "user_id": "u", "csrf_token": "c", "web_app_id": "w"}
        backend._auth = backend._hub_auth(creds)
        self.assertEqual(backend._auth.cookies, {"session": "s"})
        self.assertEqual(backend._auth.user_id, "u")
        backend._auth.save()
        self.assertEqual(saved[0]["cookies"], {"session": "s"})
        self.assertEqual(saved[0]["user_id"], "u")
        self.assertFalse(os.path.exists(os.path.join(self.home.name, "credentials.json")))


if __name__ == "__main__":
    unittest.main()
