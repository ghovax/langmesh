Which of the two mails this call sends in the thread.

- `progress` — a short status of the direction you are taking. A new email is sent. Keep
  working after this call. This is the default.
- `reply` — the answer to the person who asked. A new email is sent with this text. The
  turn ends after this call.

A short turn needs no `progress` call. Do not narrate every command. The turn is
unfinished until a `reply` lands.
