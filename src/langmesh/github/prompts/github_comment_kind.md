Which of the two comments this call writes onto the acknowledgement.

- `progress` — a short status of the direction you are taking. The acknowledgement updates in place. Keep working after this call. This is the default.
- `reply` — the answer to the person who asked. The acknowledgement updates in place with this text. The turn ends after this call.

A short turn needs no `progress` call. Do not narrate every command. The turn is unfinished until a `reply` lands.
