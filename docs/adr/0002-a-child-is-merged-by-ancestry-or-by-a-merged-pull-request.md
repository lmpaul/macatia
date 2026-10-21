# A child is merged by ancestry or by a merged pull request

A squash or rebase merge puts a child's changes on the base branch under new commits, so its verified head never reaches the base branch's history and the parent closure refused children a human had merged. A child now counts as merged when its verified head is on the base branch, or when GitHub associates it with a pull request merged into the base branch; a blocker that is implemented but still open counts as satisfied when its verified head, or that pull request's merge commit, is on the run's branch. Ralph finds the pull request from the verified head at check time, through GitHub's commit-to-pull-requests listing, and writes nothing new in the implementation evidence.

## Considered Options

- **Merged pull request only.** Rejected: ancestry stays exact and covers merges made without a pull request, such as a local merge pushed to the base branch.
- **Record the pull request number in the implementation evidence.** Rejected: evidence published before the change has none, and a human may close Ralph's draft PR and merge the work through another pull request, which would leave the recorded number wrong.
- **Accept a pull request merged into any branch.** Rejected: work merged into an unrelated branch is not integrated; a repository that integrates on another branch configures it as its base branch.
