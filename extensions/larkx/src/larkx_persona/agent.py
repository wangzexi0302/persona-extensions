"""/agent: which Feishu messages ask the assistant, and who besides the user may ask. The agent_* settings follow
Persona's docs/extensions/lark-agent.md ("What counts as a request"); the connector does the rest. Pure, for the tests."""
import re

PREFIX = "/agent"
# Every answer ends with it: a message that does is one of ours, whatever else it says.
SUFFIX = "🤖 AI 代答，仅供参考"
TYPES = ("TEXT", "POST")
# What ends a topic the assistant follows. The hub's channel pipeline stops a turn on /stop and /停 itself, so the
# connector hands /stop over for each of them.
STOP_WORDS = ("/stop", "/停止", "/停")
FOLLOW_DM, FOLLOW_ALL, FOLLOW_OFF = "dm", "all", "off"
FOLLOW_MINUTES = 30
FOLLOW_MINUTES_MOST = 1440
# LarkAgentX writes a post (rich text) as this mark, its title, then its text.
POST_MARK = "[富文本]"
# A Feishu user id: digits only. A list entry may carry a name too; only the id counts.
USER_ID = re.compile(r"\d{6,}")
LABEL_TRIM = " \t\r\n()（）[]【】<>《》:：,，;；-—·|/"
LEADING_AT_WORDS = re.compile(r"^(?:@[^\s/@]+\s*)+")


class Settings:
    """The agent_* settings, defaults filled in. The hub has checked them against the manifest's schema."""

    def __init__(self, config=None):
        c = config or {}
        self.trigger_prefix = _flag(c.get("agent_trigger_prefix"), True)
        self.prefix = (c.get("agent_prefix") or "").strip() or PREFIX
        self.self_mention = _flag(c.get("agent_trigger_self_mention"), True)
        self.owner_only = _flag(c.get("agent_owner_only"), True)
        self.allow = people(c.get("agent_allow"))
        self.deny = people(c.get("agent_deny"))
        self.deny_reply = (c.get("agent_deny_reply") or "").strip()
        follow = c.get("agent_follow")
        self.follow = follow if follow in (FOLLOW_DM, FOLLOW_ALL, FOLLOW_OFF) else FOLLOW_DM
        self.follow_seconds = 60 * _minutes(c.get("agent_follow_minutes"))

    def may_ask(self, sender, me):
        """Whether sender may call the assistant: the user always; a denied person never, even when also allowed; with
        "only me" on, the allowed people; with it off, everyone else."""
        if me and sender == me:
            return True
        if not sender or sender in self.deny:
            return False
        return sender in self.allow if self.owner_only else True

    def follows(self, kind):
        """Whether a topic in a chat of this kind ("dm" or "group") is followed without the prefix."""
        return self.follow == FOLLOW_ALL or (self.follow == FOLLOW_DM and kind == "dm")

    def summary(self):
        """The rules in force, counts only (it goes to the log)."""
        triggers = []
        if self.trigger_prefix:
            triggers.append("prefix %s" % self.prefix)
        if self.self_mention:
            triggers.append("self-mention")
        who = "only me" if self.owner_only else "everyone"
        return "agent: %s; %s, %d allowed, %d denied; deny reply %s; follow %s, %d min" % (
            ", ".join(triggers) or "no trigger", who, len(self.allow), len(self.deny), "on" if self.deny_reply else "off",
            self.follow, self.follow_seconds // 60)


def people(entries):
    """A list setting as {user id: name}: each entry is a user id, with a name for people to read if wanted
    ("张三 7000000000000000001"). An entry without an id is left out."""
    out = {}
    for entry in entries or []:
        if not isinstance(entry, str):
            continue
        ids = USER_ID.findall(entry)
        if not ids:
            continue
        name = entry.replace(ids[-1], " ").strip(LABEL_TRIM)
        out[ids[-1]] = " ".join(name.split())
    return out


def body(m):
    """The text a request is read from: a post's mark, and its title when empty, left out."""
    text = m.get("text") or ""
    if m.get("type") == "POST" and text.startswith(POST_MARK):
        head, _, rest = text.partition("\n")
        title = head[len(POST_MARK):].strip()
        return title + "\n" + rest if title else rest
    return text


def is_answer(text):
    return (text or "").rstrip().endswith(SUFFIX)


def after_prefix(text, prefix):
    """What follows prefix at the start of text (case aside; then a space, a colon, a line or nothing), or None when the
    text does not start with it: for "/agent", "/agents" does not."""
    s = (text or "").lstrip()
    if not prefix or s[:len(prefix)].lower() != prefix.lower():
        return None
    rest = s[len(prefix):]
    if rest and not (rest[0].isspace() or rest[0] in ":："):
        return None
    return _clean(rest)


def strip_leading_mentions(text, mentions):
    """text without the @-mentions it starts with (in a group, "@someone /agent …"): the mentions' own text, or, for a
    message whose mentions are not known, words starting with @."""
    s = (text or "").lstrip()
    names = sorted({x["text"] for x in mentions or [] if x.get("text")}, key=len, reverse=True)
    if not names:
        return LEADING_AT_WORDS.sub("", s)
    found = True
    while found:
        found = False
        for name in names:
            if s.startswith(name):
                s = s[len(name):].lstrip()
                found = True
                break
    return s


def mentions_of(m, user):
    return any(x.get("id") == user for x in m.get("mentions") or [])


def mentions_me(m, me):
    """Whether m @-mentions the user: Feishu's own flag on a pushed message, or the user's id among its mentions."""
    return bool(m.get("at_me")) or (bool(me) and mentions_of(m, me))


def read(m, me, settings):
    """What message m asks for, as (request text, "prefix" or "mention"), or None when no trigger that is on makes it a
    request. Says nothing of whether its sender may ask; a self-mention counts in the user's own messages only, by user
    id (never by a name), and with the prefix as well counts once."""
    text = body(m)
    mentions = m.get("mentions") or []
    rest, how = None, ""
    if settings.trigger_prefix:
        rest = after_prefix(strip_leading_mentions(text, mentions), settings.prefix)
        if rest is not None:
            how = "prefix"
    if settings.self_mention and me and m.get("sender") == me and mentions_of(m, me):
        rest = _drop_mentions(text if rest is None else rest, mentions, me)
        how = how or "mention"
    if not how:
        return None
    return rest, how


def is_stop(text, mentions=None):
    """Whether text is /stop (or /停止, /停), after any @-mentions it starts with."""
    return strip_leading_mentions(text, mentions).strip().lower() in STOP_WORDS


def _drop_mentions(text, mentions, user):
    for name in sorted({x["text"] for x in mentions if x.get("id") == user and x.get("text")}, key=len, reverse=True):
        text = text.replace(name, "")
    return _clean(text)


def _clean(text):
    return (text or "").lstrip(" \t\r\n:：").strip()


def _flag(value, default):
    return value if isinstance(value, bool) else default


def _minutes(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return FOLLOW_MINUTES
    return max(1, min(int(value), FOLLOW_MINUTES_MOST))
