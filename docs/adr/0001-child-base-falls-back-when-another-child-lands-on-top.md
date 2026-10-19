# A resumed child's base moves past children implemented on top of its earlier work

Ralph records each child's child base in a trusted marker every time a child cycle starts, and a resumed child reuses it so that its implementation evidence, review session, and local gate selection cover the commits left by earlier child cycles. When another child was implemented on the branch after that child base, the resumed child's child base moves to the latest such child's verified head instead: its implementation evidence names the earlier child cycles' commits it no longer covers, while local gates are still selected from every path changed since the recorded child base, a safe superset. We accept that the review session never sees those earlier commits, because the alternatives cost more than the case is worth.

## Considered Options

- **Hand the child back to human review when another child landed on top.** Rejected: implementing other children while one is in human review is how runs normally proceed, and the human could only untangle it by rewriting history.
- **Cover several commit ranges per child.** Exact, but it needs a second marker for where each child cycle ended, a multi-range implementation evidence format that every later parser must accept, and a review prompt over several diffs, all for a history order Ralph does not control. Worth revisiting only if the uncovered case proves common.
