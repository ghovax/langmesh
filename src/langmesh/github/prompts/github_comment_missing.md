You stopped without finishing the GitHub comment. Writing the reply in prose is not submitting it — nothing is posted until the tool call itself lands.

Call `submit_github_comment` with the entire final comment in `comment` and `done` true. Do not call any other tool, do not write more prose, and do not continue the conversation: that call is the only accepted finish, and you will be asked again, with the same conversation in front of you, until you make it. Progress notes you already posted are not the finish.
