# Project Operating Rules

These rules apply to the entire repository.

## GitHub write boundary (mandatory)

- The only authorized GitHub write target for this checkout is the owner's repository: `https://github.com/Jason1Jiang/chatgpt2api` (`origin`).
- Treat `https://github.com/basketikun/chatgpt2api` (`upstream`) as read-only. It may be fetched or pulled only to synchronize source changes.
- Never push commits, branches, tags, releases, or other local modifications to `upstream`.
- Never open, update, or otherwise submit pull requests, issues, comments, or releases against `upstream` on behalf of this project.
- Interpret requests such as "commit to GitHub", "push", or "publish" as a direct commit and push to the owner's `origin` repository. Do not create a pull request unless the user explicitly asks for one, and any requested pull request must remain within the owner's repository.
- Before every GitHub write operation, verify that the destination resolves to `Jason1Jiang/chatgpt2api`. Stop instead of writing if the destination is different or ambiguous.
- Do not commit credentials, tokens, private configuration, or machine-specific secrets, even to the owner's repository.

Expected remote layout:

```text
origin    https://github.com/Jason1Jiang/chatgpt2api.git
upstream  https://github.com/basketikun/chatgpt2api.git
```
