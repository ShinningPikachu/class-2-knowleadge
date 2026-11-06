# Class Knowledge for DeepSeek Harness

`dsh-class2knowledge` connects the Class Knowledge macOS app and its local study library to DeepSeek Harness (DSH). Its panel follows the app's **Library, Agent, Lecture Notes, Job Queue, and Settings** organization, opens in Study Chat, and keeps the same subject → folder → file flow in Library.

## Features

- **Library:** browse subjects and folders, search indexed text, and read source passages and locations.
- **Study Chat:** ask a question directly in the plugin, scoped to the current document, current subject, or all subjects. The active Harness model receives relevant indexed passages, cites them as `[S1]`, `[S2]`, and can use its general subject knowledge when the files do not answer the question.
- **Agent:** see the Harness bridge state and use the native app's Agent workspace with the same active Harness model.
- **Lecture Notes and Job Queue:** follow recent lecture jobs, progress, and queue states.
- **Settings:** inspect workspace, app, bridge, and selected model status; edit settings in the native app.
- **Harness tools:** `classlib_status`, `classlib_subjects`, `classlib_jobs`, `classlib_search`, `classlib_start`, and `classlib_stop`.
- **Native app controls:** open Class Knowledge or request a normal quit from the panel or tools.
- **DSH agent bridge:** the native app can send a question to the active Harness agent. Health checks return locally and do not start a model turn.
- **Local data:** the panel reads `library/library.sqlite3`, `jobs/jobs.sqlite3`, and a small allowlist of model settings; it does not modify library or queue records.

Search uses SQLite `LIKE` against extracted document chunks. It is literal for `%`, `_`, and `\\`, supports an optional subject filter, and returns document IDs, names, subjects, locators, and snippets. It is keyword search rather than semantic retrieval.

## Install in a DSH profile

Run from the Harness profile directory:

```sh
pnpm add file:/path/to/class-2-knowleadge/dsh-plugin
```

Add `dsh-class2knowledge` to `dsh.profile.bundles` in that profile's `package.json`, then restart or reload the profile. For this workspace, the desktop profile already has the local package dependency and bundle entry.

The plugin expects the Class Knowledge app at `~/Applications/Class Knowledge.app` and the library at this workspace's `library/library.sqlite3` by default. Set the environment variables below in the DSH profile to use different locations.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLASS2KNOWLEDGE_ROOT` | `/Users/jinhenglin/Documents/work/class-2-knowleadge` | Project and library root |
| `CLASS2KNOWLEDGE_APP_BIN` | `~/Applications/Class Knowledge.app/Contents/MacOS/ClassKnowledgeApp` | App executable |
| `CLASS2KNOWLEDGE_APP_PATH` | Derived from `CLASS2KNOWLEDGE_APP_BIN` | `.app` bundle passed to macOS `open` |
| `DSH_CLASS_AGENT_SOCKET` | `~/.dsh/class-knowledge-agent.sock` | Private Unix socket between DSH and the app |
| `DSH_CLASS_AGENT_TIMEOUT_MS` | `180000` | Maximum duration for one agent request |

The app talks to its Python backend through JSONL on stdio. The plugin uses `pgrep` to report whether the app is running, and `open -a` / AppleScript for app launch and normal quit.

## Development

The plugin requires Node.js 22.5 or newer and the macOS `sqlite3` command for its library endpoints.

```sh
node --test dsh-plugin/test/*.test.js
```

The desktop Harness profile bundles its own Node runtime; use that runtime to run the same test command if Node is not on `PATH`.

The plugin entry point is `lib/index.js`; its DSH UI client is `lib/client.js`. `cordis.patch.yml` registers the client panel in Harness. The plugin can still provide its navigation, local library, and queue views in profiles that do not expose the `agents` service; only the app-to-Harness agent bridge requires that service.
