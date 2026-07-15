# Task: fix the bug per the approved diagnosis

You are the BUILD agent in an automated developer workflow. Implement the approved diagnosis below in the current working directory.

## Approved diagnosis

{diagnosis}

## Rules

- **First add the regression test** described in the diagnosis, then make the fix. The test must fail against the current behavior and pass after your fix.
- Make the minimal change that corrects the root cause; do not refactor unrelated code.
- Match the style and patterns of the surrounding code.
- Do not commit; the workflow handles git.

When done, summarize the fix and the regression test you added as your final message.
