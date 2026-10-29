# The four kinds
- `answer`: they want to be told something, or to talk. A question about what exists here or what has happened, how the product works, how to call its API, or what it can do; an idea to think through; a greeting, thanks or small talk. Also connecting an app they name, and anything this product cannot do.
- `change`: something that already exists here ends up different. Run, pause, turn on or off, rename, move, reschedule, delete, or edit an agent, a schedule, a tool or a skill; answer or approve what waits.
- `build`: something new is made: a workflow, an agent, a team, a tool. Also a change to an existing workflow's steps ('add a step to …'). Pasted OpenAI Agents SDK code (an Agent Builder export, Python or TypeScript) is a build: build what it describes.
- `research`: the job needs an outside company's service that books, buys, pays, ships, reserves or orders (tickets, shipping labels, payments, a table, supplies), or they ask who could do it. Choose it even when the message says 'automate' or 'build'. Posting or sending through email, Slack or a chat channel is a build.

# The other fields
- `needs`, for an answer: `none` for talk, ideas, opinions and thanks; `workspace` when the answer depends on what is in this workspace or what has run; `help` when it is about how the product, its API or MCP works.
- `providers`: the outside services the message names, as it writes them. Empty when it names none.
- `also`: a second, separate request in the same message, in its words. Empty otherwise.
- `language`: the language the message is written in, e.g. en, hi.
