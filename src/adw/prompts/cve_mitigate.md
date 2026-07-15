# Task: implement the protection

You are resuming your session. You just added a security regression test that fails because the vulnerability is present. Now implement the **protection** described in the approved analysis so that the vulnerability is closed and your security test passes.

## Approved analysis

{analysis}

## Rules

- Implement the minimal defensive change that closes the hole (input validation, escaping/parameterization, a safe API, a safe default). Do not weaken or delete the security test to make it pass.
- Do not break existing behavior; the full suite must still pass.
- Match the style and patterns of the surrounding code. Do not commit.

As your final message, describe the protection you implemented and confirm the security test now passes.
