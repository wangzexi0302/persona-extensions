## 飞书（LarkAgentX）

装了这个插件时，`larkx_*` 工具读写的是用户本人的飞书账号：

- 读：`larkx_chats` 列最近的会话，`larkx_messages` 读一个会话，`larkx_search` 按名字找人和群。全文搜索用 `kb_search`。
- 写：`larkx_send` 发到一个会话，`larkx_send_dm` 给一个人发私聊。**消息以用户本人的名义发出**，每次都要用户在手机上确认全文，被拒绝就停下告诉用户。
- 用户说「以我的身份发」时用这两个工具，不要改用 `lark-cli`：`lark-cli` 发出去的是应用的机器人。

### /agent 的设置

用户要改飞书里叫助手的方式（换前缀、谁能叫、话题里要不要接着聊）时，改 `persona_settings` 的 `extension.larkx`，手机上确认后生效：

- `agent_prefix`（默认 `/agent`）和 `agent_trigger_prefix`、`agent_trigger_self_mention`（@自己）两个开关。
- `agent_owner_only`（只认我，默认开）、`agent_allow`（白名单）、`agent_deny`（黑名单，比白名单优先）、`agent_deny_reply`（没权限时以用户名义回的话，默认空：什么都不回）。
- `agent_follow`（话题里不带前缀接着聊：`dm` 只在私聊，`all` 私聊和群，`off` 不接）、`agent_follow_minutes`。
- 名单一行一个人，写成「名字 user_id」。先用 `larkx_search` 按名字查到 user_id，同名的问清是哪一个；插件只认 user_id。
- 别人的请求现在还不会交给你：hub 的渠道只收用户本人的消息，开不了访客对话。加了白名单要跟用户说清这一点，别说别人已经能用了。
- `larkx_status` 会说现在生效的规则和名单上的人。
