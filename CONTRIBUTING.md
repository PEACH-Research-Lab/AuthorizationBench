# Contributing

AuthorizationBench accepts fixes to task bundles, sandbox services, harness
adapters, evaluation code, and documentation.

Before opening a pull request:

1. do not commit provider keys or a populated `config.toml`
2. run the offline unit tests
3. keep task IDs and taxonomy IDs stable
4. document any change that alters task completion or authorization criteria
5. include a regression test for service or evaluator behavior

New tasks should define the principal's instruction, completion criteria, and
case-specific out-of-authorization actions separately. Both the authorized and
out-of-authorization paths must be executable and observable in the sandbox.

Issues that report task ambiguity should identify the task ID and whether the
ambiguity concerns completion, authorization, or environmental realization.
