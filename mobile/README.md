# LangMesh, on a phone

An Expo client for the LangMesh daemon. It is a **client** — it contains no harness and starts no daemon. The Mac has to be awake and `langmeshd` running, and the daemon's door has to be reachable from the phone.

## Running it

```sh
bun install
bun run start
```

Then install Expo Go on your phone and scan the code. Everything the app uses — the camera for pairing, the keychain, the microphone — is in Expo Go, so no native build is needed.

## Pairing

The app pairs by pasting (or scanning) the `langmesh://pair#…` link the daemon's door produced. The link carries the address and a token, and both stay on this phone; pair it the same way you would add a machine under **Settings, then Connection** in the desktop app.

`bun run web` renders the same app in a browser via React Native Web, which is useful for looking at layout. The camera does not work there — pair by pasting the `langmesh://pair#…` link.

## Checks

```sh
bunx tsc --noEmit
bunx expo lint
```

The event types are generated into `shared/` by the web client's `bun run check:events`, and this client imports them — so there is nothing here to check for drift.

## What is in here

Almost nothing. The interface is `web/`, and this app is a window onto it — plus the two things a page cannot do for itself: reading a pairing code with the camera, and keeping the token in the keychain. If you are looking for the sessions list or the composer, they are in `web/src`.

Which means: **UI work happens in `web/`, not here.** A dialog that is unusable at 390pt is unusable in a narrow browser window too, so it is fixed in one place.

## Notes for whoever edits this next

**Expo has changed.** Read the versioned docs at
<https://docs.expo.dev/versions/v57.0.0/> rather than working from memory. This is SDK
57, React Native 0.86, the New Architecture, React Compiler on.

**Do not add screens here.** This directory held a React Native port of the whole interface and it was deleted, because a port can be faithful the day it is written and cannot stay faithful. If a screen belongs in LangMesh, it belongs in `web/src` where both clients get it.

**The token becomes a cookie.** The app opens the interface once with `?token=…`; the door answers with an `HttpOnly` session cookie that covers every subsequent request. Nothing in the page ever holds the token.
