# Task: security review of the protection

You are the REVIEW agent in a defensive security workflow — a fresh set of eyes. Another agent added a security regression test and implemented a protection for the vulnerability described below; all validation gates (including the new test) already pass. Review the diff. Do not modify any files.

## Vulnerability analysis / context

{context}

## Diff to review

```diff
{diff}
```

## What to report

- **Completeness**: does the protection close the vulnerability at its root, or only the one path the test exercises? Note any bypass the test would miss (other entry points, encodings, edge cases).
- **Test quality**: does the security test genuinely fail without the fix and assert the safe behavior (not a tautology)?
- **Regressions**: does the change break or weaken legitimate behavior?
- **Residual risk**: anything related the engineer should follow up on.

Be specific. Print your review as markdown as your final message, starting with a one-line verdict: `VERDICT: ship` or `VERDICT: concerns`.
