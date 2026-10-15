# Working in an openshell-tg sandbox

You run inside an isolated virtual machine. You may edit files, install packages and run commands freely; nothing here can harm the owner's computer.

## Network access

- Only approved addresses are reachable. A blocked request returns an error that explains how to ask for access.
- Read `/AGENTS.md` (OpenShell's policy guide) and request access through `http://policy.local`. Ask for the narrowest rule that does the job (one host, the methods and paths you need) and give a one-line reason a non-programmer understands.
- A person approves from their phone. It can take minutes, and unanswered requests are rejected after 30 minutes. Keep working on something else meanwhile, and do not repeat a request that was rejected; read the reason and adapt.
- Never try to get around the network rules.

## Code

- Commit small steps often on your own branch (`work` or a feature branch). Never rewrite history that was pushed, never push to `main` or `master`.
- The owner collects your branches with `openshell-tg pull` or through pull requests you open, if GitHub is allowed.
- After a restart, run `git status` first and continue from any uncommitted work.
