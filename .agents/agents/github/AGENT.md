---
name: github
title: GitHub
description: Does the work asked in a GitHub mention, in the repository that comment is on.
role: primary
enabled: true
model: claude-sonnet-4-5
provider: anthropic
reasoning_effort: high
permission_mode: automatic
---

This profile is the mention Action's model and provider. The job keeps its own
system prompt and tools; change `model` and `provider` here to pick what it calls.
The API key is the repository secret `LANGMESH_API_KEY`, written to a secret file
at the start of the job. Do not put a key in this file.
