# Android fork changelog

What this fork adds on top of the upstream plugin, newest first. The plugin's version (plugin.yaml,
dashboard/manifest.json) and CHANGELOG.md always follow upstream: the nightly sync resets them, so a fork
change never bumps the version and is written here instead. Older entries carry the number the fork used
before it followed upstream's numbering; those numbers are not upstream's releases of the same name.

## 2026-11-16 (on upstream 0.177.19)

### New and improved
- Boards Hermes draws in a chat (tables, charts, stats and the rest of `conduit_show`'s cards) are listed in Conduit's Artifacts with the files, pages and links, under their own Boards filter, each with its cards so Conduit can draw it again. A board's name is the title Hermes gave it. A status card Hermes kept up to date (a `live` card) is one board, listed at its newest. Only a Conduit that asks for boards gets them (capability `artifact-boards`), so the App Store app's lists are unchanged.

## 2026-11-15: numbered 0.165.0 in the fork

### New and improved
- Conduit's model sheet can start Favorites with the models you use most, counted by requests the way usage.asir.dev counts them and weighted to the last few weeks, so a model you stopped using drops out and a fallback's few requests never make it a favorite.

## 2026-11-15: numbered 0.164.0 in the fork

### New and improved
- Conduit's crash and freeze reports can come straight to this host instead of waiting for you to review and share them. Repeats of one problem are grouped, and a scheduled Hermes job can investigate each new one and queue a fix.

## 2026-11-15: numbered 0.163.0 in the fork

### New and improved
- Conduit's new Hermes page can show and edit what Hermes remembers: Memory and User profile entries with their character limits, and OpenViking memories to browse, search, read and forget. Edits go through Hermes' own memory store, so its limits and safety scan still apply, and an entry that changed in the meantime is never overwritten.

## 2026-11-15: numbered 0.162.5 in the fork

### Fixes
- A scheduled job that reports in a Conduit chat (`deliver: bot-chat`, a topic chat included) now notifies from that chat, whether or not it runs an agent. Before, an agent job's only notification came from its run's own hidden chat, so its tap opened an empty-looking run instead of the chat with the report.

## 2026-11-15: numbered 0.162.4 in the fork

### New and improved
- A notification can open a screen of Conduit instead of the chat its note is in. `hermes conduit-push say --open conduit://meds ...` sends a medication reminder whose tap opens Meds; the link travels sealed with the notification. A Conduit that doesn't know the link opens the chat, as before.

## 2026-11-15: numbered 0.162.3 in the fork

### New and improved
- Scheduled reports can go to topic chats instead of piling up in the Bot Chat. File a job under a topic in `conduit-cron-topics.json` (`{"jobs": {"<job id>": "<topic>"}, "chats": {"<topic>": "<chat id>"}}`) and its report shows in that topic's chat, with its notification opening that chat. A job that isn't filed, a topic chat that was deleted or archived, and a job set to "Let the bot act on it" keep the Bot Chat.

## 2026-11-13: numbered 0.162.2 in the fork

### Fixes
- A script-only scheduled job delivered to the Bot Chat now sends a notification with its report, titled with the job's name. Before, its report showed in the Bot Chat with no notification, so an alert from such a job went unseen.

### New and improved
- Scripts can post a message into the Bot Chat with a notification: `hermes conduit-push say --title <title> <text>`. Useful for something that can't wait for a job's run to end, like a Duo number.

## 2026-11-11: numbered 0.162.1 in the fork

### Fixes
- Android notification deliveries stop after unregister, key replacement, disabling notifications or plugin unload. A phone notification cannot park a relay-only clarification.

### New and improved
- This Android fork adds encrypted UnifiedPush delivery, scoped registration, room invites and private Pulse notification levels on the current host plugin.
- Android and Wear OS can hold a visible chat independently of Apple devices. Typed watch messages stay typed, and Android Auto receives driving-appropriate reply guidance.
