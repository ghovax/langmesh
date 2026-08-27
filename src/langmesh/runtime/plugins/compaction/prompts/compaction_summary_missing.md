You stopped without submitting the compaction summary. Writing or restating the handoff
in prose is not submitting it — nothing may continue until the tool call itself lands.

End your turn by calling `submit_compaction_summary` exactly once, as your final action,
with the entire handoff summary in its `summary` field. Do not call any other tool, do
not write more prose, and do not continue the conversation: the call is the only
accepted answer, and you will be asked again, with the same conversation in front of
you, until you make it.
