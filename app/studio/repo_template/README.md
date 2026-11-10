# Bookly support agent

The configuration of Bookly's AI support agent, kept as code. Bookly Studio
writes this repo: every version published in Studio opens a pull request,
and deploying it to 100% merges the PR and tags it `agent-vN`. So `main`
is always what's in production, and the tags are its release history.

## Layout

| Path | What it is |
|---|---|
| `agent/agent.yaml` | Models, limits, which tools are on, and the AOP load order |
| `agent/prompts/` | The system prompt, the scope-gate prompt, and the decline message |
| `agent/aops/*.md` | One file per Agent Operating Procedure: front matter, then the steps |
| `agent/catalog.yaml` | What the platform provides: the coded tools and customer metadata fields |
| `tests/*.yaml` | The test suite Studio runs before a release |

In AOPs, `@tool` calls a coded tool, `#Other AOP` hands off to another
AOP, and `{{ field }}` reads customer metadata.

## Checks on every pull request

- **Bookly Studio / release gate**: Studio posts this. It passes when the
  full test suite has passed on this exact configuration.
- **Validate agent / AOP references**: GitHub Actions runs
  `scripts/validate.py`, which checks that every reference resolves.

## Editing outside Studio

Edit an AOP or prompt here and merge it to `main`, then use **Pull from
main** on Studio's Releases page to bring it into the draft, test it, and
release it like any other change.
