Submit the durable summary that replaces the conversation compacted away during compaction.

- Call it only when the compaction instruction asks for it, with the entire summary in its `summary` field.
- Make it the final call rather than part of a parallel batch. Outside that instruction this tool is inert and records nothing.
