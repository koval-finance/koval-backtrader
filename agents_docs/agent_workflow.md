# Agent workflow

The loop, the git policy, and the rules that do not bend.

## The loop

1. **Restate.** Say what the task is and what is explicitly out of scope
   before editing anything. Most bad changes here are correct changes to the
   wrong thing.
2. **Read the invariant first.** If the task touches licensing, the entry
   point, or the Backtrader coupling, read
   [invariants.md](invariants.md) before writing code, not after a guard
   fails.
3. **Write the failing test.** Watch it fail. A test that has never been red
   has not been shown to test anything.
4. **Implement the minimum.** No drive-by refactoring, no adjacent cleanup, no
   renaming on the way past.
5. **`./scripts/verify.sh`.** Exit code 0 is the definition of done.
6. **Review your own diff**, report what changed and what you did not do, and
   stop.

## Git policy

Agents never commit, push, tag, rebase, merge, or rewrite history. Prepare the
change, run verification, suggest a commit message, and stop. Every git write
belongs to a human.

Contributions use DCO sign-off (`git commit -s`); CI rejects commits without
it.

## Rules that do not bend

- **Fix the cause, never the guard.** When a guard test fails it has found
  something. Editing the test to pass is the one change that is never correct.
- **No new runtime dependency** without an issue first. The list is
  `koval-engine`, `backtrader`, `numpy`, `pandas`, and it is short on purpose.
- **Never import application code.** This package sees the engine and nothing
  above it.
- **Never create `src/koval/`.** See [invariants.md](invariants.md) for why
  the obvious-looking namespace is the wrong one.
- **English only** in code, comments, docstrings, tests, and docs.

## Reporting

A finished task reports: what changed, which tests were written and observed
failing, the `verify.sh` result, anything deliberately left undone, and the
suggested commit message. Report failures as failures — a hedged "should be
working" is worse than a plain "this test is red and here is the output".

## Update this file when

The loop, the git policy, or the dependency rule changes.
