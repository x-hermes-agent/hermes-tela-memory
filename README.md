# hermes-tela-memory

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider that keeps the agent's long-term memory in tela wiki pages.

- A fixed list of tela pages is fetched **once per session** and injected into the system prompt as a frozen snapshot, the same way the built-in `MEMORY.md` / `USER.md` work. Fetching costs no LLM tokens, and the prompt prefix stays cacheable.
- The agent edits those pages with the `tela_memory` tool (`read`, `add`, `replace`, `remove`).
- People read and edit the same pages in tela. tela's revision history shows who changed what; agent edits are marked with source `agent`.
- If tela is unreachable, the last cached copy is used and marked as stale in the prompt.

- Optional weekly maintenance (separate plugin): tidies the pages and sends a short summary of what changed.

Pure Python standard library; no extra dependencies.

## Plugins in this repo

| Folder | Kind | What it does |
|---|---|---|
| `tela/` | memory provider | Loads the pages into the prompt and adds the `tela_memory` tool |
| `tela-maintenance/` | standalone | Adds the "Weekly memory maintenance" Automation Blueprint |

The maintenance blueprint is a separate plugin because Hermes does not load memory providers during general plugin discovery, so a blueprint registered by the provider would not reliably appear in the catalog.

## Install

```bash
hermes plugins install x-hermes-agent/hermes-tela-memory/tela
hermes plugins install x-hermes-agent/hermes-tela-memory/tela-maintenance --enable   # optional
```

## Configure

Only the tela URL and a personal access token **with write scope** are required. On first start the plugin finds or creates a space called `Memory` and four starter pages in it (`User`, `Agent Notes`, `Ongoing Work`, `Map`), then saves their ids to `$HERMES_HOME/tela_memory.json`.

| Setting | Env var | `tela_memory.json` key | Default |
|---|---|---|---|
| tela personal access token | `TELA_API_KEY` | (env only) | required |
| tela base URL | `TELA_URL` | `url` | `TELA_MCP_URL` without `/api/mcp` |
| Pages, in prompt order: ids and/or titles | `TELA_MEMORY_PAGES` (`41,User,43`) | `pages` | the four starter pages |
| Space for pages given by title: id or name | `TELA_MEMORY_SPACE` | `space` | `Memory` |
| Org that owns the space when it is created | `TELA_MEMORY_ORG_ID` | `org_id` | personal |
| Per-page size cap for writes | `TELA_MEMORY_MAX_CHARS` | `max_chars` | `4000` |
| HTTP timeout (seconds) | `TELA_MEMORY_TIMEOUT` | `timeout` | `8` |

Pages given by title are looked up in the space (case- and accent-insensitive) and created when missing. Env vars win over the file; resolved ids are saved only when pages and space come from the file. `hermes memory setup` (select `tela`) writes the file for you.

Activate it in `config.yaml`, and turn off the built-in stores so the old `MEMORY.md` / `USER.md` are not injected as well:

```yaml
memory:
  provider: tela
  memory_enabled: false
  user_profile_enabled: false
```

Restart Hermes. Changes to the pages show up in the next session.

## Weekly maintenance

With `tela-maintenance` enabled, schedule it from any chat with `/blueprint weekly memory maintenance` (or from the dashboard's Automation Blueprints). You choose the day, time and where the summary goes. It is never scheduled automatically, because every run uses model tokens.

## Notes

- Keep the pages short: everything on them is sent with every request. Put long details on other tela pages and leave a one-line pointer.
- Writes are scanned with Hermes' own memory injection scanner and use tela's optimistic locking (`base_version`), so concurrent human edits are not overwritten.
- Pages are cached under `$HERMES_HOME/cache/tela_memory/`.

## License

MIT
