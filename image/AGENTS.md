# Working in an openshell-tg sandbox

You run inside an isolated virtual machine. You may edit files, install packages and run commands freely; nothing here can harm the owner's computer.

## Network access

- Only approved addresses are reachable. A blocked request fails with `policy_denied`.
- Do not stop there. Ask for the address through the local API at `http://policy.local`. OpenShell's full guide is `/etc/openshell/skills/policy_advisor.md`; read it if it exists. In short:
  1. `curl -s http://policy.local/v1/denials?last=10` shows what was blocked.
  2. `curl -s http://policy.local/v1/policy/current` shows what is allowed now.
  3. Submit the narrowest rule that does the job:
     `curl -s -X POST http://policy.local/v1/proposals -H 'Content-Type: application/json' -d '{"intent_summary":"<one line a non-programmer understands>","operations":[{"addRule":{"ruleName":"<name>","rule":{"name":"<name>","endpoints":[{"host":"<host>","port":443,"protocol":"rest","enforcement":"enforce","rules":[{"allow":{"method":"GET","path":"<path>"}}]}],"binaries":[{"path":"<program path>"}]}}}]}'`
  4. Save `accepted_chunk_ids`, then wait: `curl -s "http://policy.local/v1/proposals/<chunk_id>/wait?timeout=300"`. When `status` is `approved`, retry. When `rejected`, read `rejection_reason`, adapt, and do not repeat the same request.
- Ask for one host, the methods and paths you need, and the program that makes the request. A person approves from their phone; it can take minutes, and unanswered requests are rejected after 30 minutes. Keep working on something else meanwhile.
- Never try to get around the network rules.

## Code

- Commit small steps often on your own branch (`work` or a feature branch). Never rewrite history that was pushed, never push to `main` or `master`.
- The owner collects your branches with `openshell-tg pull` or through pull requests you open, if GitHub is allowed.
- After a restart, run `git status` first and continue from any uncommitted work.
