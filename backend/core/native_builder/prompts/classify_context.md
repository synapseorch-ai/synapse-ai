The message:
{{ state.user_message }}

The conversation so far:
{{ state.chat_history }}

The context says which workflow the message is about, as the server checked it: `current_orchestration_name` and `current_orchestration_source`, empty when there is none. `viewing` means it is open on their screen, so 'this' and 'it' mean it. `named` means the message names it. `picked` means the previous turn asked which one and this message chose it: the request is the previous user message in the conversation. `built` means this conversation built or changed it last, so 'this' and 'it' mean it: 'run it' runs it, and more detail is a change to it. `ambiguous` means more than one fits.
