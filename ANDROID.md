# Android companion host plugin

Source: `AsirShahid/hermes-conduit-notifier`, branch `android`. The phone app is `AsirShahid/hermes-conduit`, branch `android`.

## Differences from upstream 0.151.0

- Authenticated, profile-scoped `/push/android/register` and `/push/android/unregister`. Registration stores endpoints and per-phone secrets in an owner-only file. Status returns no endpoint or secret.
- UnifiedPush delivery retains the app's existing E2E protocol and optional RFC 8291 Web Push encryption. The Apple relay receives no Android endpoint or Android key.
- Room invitations use their own `room_invite` preference. Pulse interruption level is inside authenticated content. Location and map requests follow the phone's explicit data-sharing settings.
- Android jobs are bounded, re-check registration and preferences before delivery, and stop after unregister, replacement or plugin unload. Idle threads retire. An Android-delivered clarification never starts an Apple relay answer poll.
- `chat-on-screen-android` accepts `android` and `wear`, independently of Apple device holds. `turn-surface-android` accepts `android`, `wear` and `android_auto`; typed watch messages remain typed.

Android delivery builds on scasellas' Android fork at `68c90e7`, retaining attribution and its protocol tests. The current upstream module layout, scoped delivery contexts, Apple relay retry behavior and current features remain intact.

## Deployment

Use a pinned commit from this branch. Back up the installed plugin and its source metadata before replacing it. Keep runtime state in the profile home, outside this checkout. Do not copy or publish push registration files.

Set `CONDUIT_PUSH_MANAGED` to `Android companion fork` in the dashboard's environment when deployment is managed outside the app. This prevents the app from pulling a different plugin build. The upstream managed-update route may still request restarts when a new version is waiting; use the host's own maintenance process for activation.

On Asir's VPS, never restart or reload the live gateway directly. Queue activation with `hermes-reload-when-idle` after the reviewed files and configuration are ready. Check the dashboard service separately, because it loads API routes at startup. This repository does not automatically deploy or activate changes.

The phone uses the existing dashboard sign-in. Push registration needs a UnifiedPush distributor on the phone and an endpoint returned by that distributor. Merely installing this plugin sends nothing and enables no phone data sharing.

## Validation

The [full CI run at `24bb938`](https://github.com/AsirShahid/hermes-conduit-notifier/actions/runs/42707089746) passed the plugin, relay and mailbox suites on 2026-11-09. Focused location, maps, Android push and calls tests also passed 387 tests, including Android-only request delivery when the Apple relay refuses or raises an error.

The [real Hermes v0.23.0 contract run at `fd24f05`](https://github.com/AsirShahid/hermes-conduit-notifier/actions/runs/42709708705) passed 81 tests with four expected failures. It includes unauthenticated Android registration rejection and separate authenticated profile registrations through an actual dashboard/plugin worker. Independent backend fixtures run in separate processes to avoid competing for Hermes' machine lease.

These checks do not prove delivery through a physical phone distributor or a live Wear OS device. Live deployment and phone-side registration are separate steps.
