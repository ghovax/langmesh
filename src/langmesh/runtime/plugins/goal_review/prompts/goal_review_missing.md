You stopped without submitting the independent verdict. Writing about your conclusion in
prose is not submitting it — nothing is settled until the tool call itself lands.

End your turn by calling `submit_goal_review` exactly once, as your final action, with
all required fields (`assessment`, `unmet`, `evidence`/`blocker`, `goal_contract`,
`standing`, `message` as appropriate for the standing you choose). Fields the standing
code does not use must be omitted (`null`). Do not call any other tool, do not write
more prose, and do not continue the investigation: the call is the only accepted answer,
and you will be asked again, with the same conversation in front of you, until you make
it.
