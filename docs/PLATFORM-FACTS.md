# Discord platform facts this bot relies on

Checked on 8 November 2026 against Discord's official developer documentation (the old
`discord.com/developers/docs/...` addresses now redirect to `docs.discord.com/developers/...`).
Each fact says where it is used. **Status of every fact: documentation only.** They are
built into the mock (`support_bot/mock_discord.py`) and the tests, but none has been
confirmed against a real Discord application yet. The live test will do that.

## Interactions endpoint security (Interactions > Overview)
- Every request carries `X-Signature-Ed25519` and `X-Signature-Timestamp`. The signature is Ed25519
  over the timestamp followed by the raw body, checked with the application's public key (hex).
  Used in `signature.py`; the mock signs exactly this way.
- A request that fails the check must get a **401**.
- Saving the endpoint URL sends a `PING` (type 1), which must get a 200 with `{"type": 1}` (PONG).
- Discord also runs routine checks that send **deliberately invalid signatures**, and removes the
  endpoint URL if they are accepted. `DiscordSide.endpoint_check()` repeats both checks.
- Not documented: a maximum age for the timestamp. The bot adds its own 5-minute replay window on
  top (`SIGNATURE_MAX_AGE_S`, 0 switches it off).

## Interaction and response types (Receiving and Responding)
- Interaction types: PING 1, APPLICATION_COMMAND 2, MESSAGE_COMPONENT 3, AUTOCOMPLETE 4, MODAL_SUBMIT 5.
- Callback types used: PONG 1, CHANNEL_MESSAGE_WITH_SOURCE 4, DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE 5,
  UPDATE_MESSAGE 7 (components only). Ephemeral flag `1 << 6` (64).
- The first response is due within **3 seconds**, or the token is invalidated. The interaction token
  is valid for **15 minutes** for follow-ups.
- Edit the original response: `PATCH /webhooks/{application.id}/{interaction.token}/messages/@original`.
  Follow-up: `POST /webhooks/{application.id}/{interaction.token}`. These routes use the interaction
  token, not the bot token, and are not bound to the bot's global rate limit.

## Application commands (Application Commands)
- Bulk overwrite: `PUT /applications/{id}/guilds/{guild.id}/commands` (instant) and
  `PUT /applications/{id}/commands` (global). It replaces **all** command types.
- Command types CHAT_INPUT 1, USER 2, MESSAGE 3. Option type STRING 3.
- Names 1 to 32 characters; slash command descriptions 1 to 100; USER and MESSAGE commands must have
  an empty description; at most 25 options. Checked in `commands.validate()` and by the mock.
- MESSAGE commands deliver the target in `data.target_id` and `data.resolved.messages`.
- Limit: 200 command creates per day per guild.

## Threads (Channel resource)
- `POST /channels/{id}/threads` starts a thread without a message. Its `type` **defaults to
  PRIVATE_THREAD (12)**, so the bot always asks for PUBLIC_THREAD (11). The mock keeps the
  documented default, so forgetting the type fails a test.
- `auto_archive_duration` is one of 60, 1440, 4320, 10080. Thread names are 1 to 100 characters.

## Rate limits (Topics > Rate Limits)
- Headers: `X-RateLimit-Limit`, `-Remaining`, `-Reset`, `-Reset-After` (seconds, may have decimals),
  `-Bucket`; on a 429 also `X-RateLimit-Global`, `X-RateLimit-Scope` and `Retry-After`.
- 429 body: `message`, `retry_after` (seconds, float), `global`, optional `code`.
- Global limit 50 requests per second per bot. 10,000 invalid requests (401, 403, 429) per 10 minutes
  gets an IP temporarily blocked, which is one more reason to stop on 401 instead of retrying.

## Messages (Message resource)
- Content up to 2000 characters. `allowed_mentions.parse` is **mutually exclusive** with `users` and
  `roles` (the mock rejects the mix). In interaction responses and webhooks the default is
  `{"parse": ["users"]}`, so the bot always sends its own `allowed_mentions`.

## Gateway (Topics > Gateway), only with ENABLE_GATEWAY=true
- Intents: GUILDS `1 << 0`, GUILD_MESSAGES `1 << 9`, MESSAGE_CONTENT `1 << 15`. Message Content,
  Server Members and Presence are **privileged** and must be switched on in the Developer Portal.
- Close code 4014: a privileged intent not enabled or approved; 4004: authentication failed.
- Without Message Content, message `content` arrives empty (except DMs and messages that mention the
  app). The mock blanks it the same way, which is why only the mention rule works without it.
- Not verified on the page: the exact wording of the close-code table (it lives on the opcodes page).

## Authentication
- Bot requests send `Authorization: Bot <token>`. The canonical reference page was not fetched; this
  comes from the examples on the Application Commands page.
