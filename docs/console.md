# The console

Run `ifc-console` without a subcommand to open the control surface for models,
permissions, clients, and browser tools. Chat happens in your MCP client or the
optional browser panel.

```text
+------------------------------------------------------------+
| model.ifc | IFC4 | MODE: ASK | MCP 127.0.0.1:8383          |
|                                                            |
| 14:02:11  ok  get_ifc_project_info  212ms                  |
| 14:02:19  ok  query_elements           48ms                |
|                                                            |
| > /mode _                                                  |
+------------------------------------------------------------+
```

## Layout and keys

- **Status:** model, mode, dirty state, endpoint, and viewer.
- **Feed:** server events and every AI operation.
- **Prompt:** slash commands with completion.

| key | action |
| --- | ------ |
| ++tab++ | insert a completion |
| ++up++ / ++down++ | move through choices or history |
| ++enter++ | select or run |
| ++escape++ | close the menu or clear the line |
| ++page-up++ / ++page-down++ | scroll the feed |
| ++ctrl+l++ | clear the feed |

Type `/` to browse commands. Values for `/mode`, `/viewer`, `/connect`, and
`/settings` complete too.

## Commands

### Models

| command | use |
| ------- | --- |
| `/file [path]` | pick or open the active model |
| `/workspace [dir]` | browse and select related files |
| `/models` | list models and attachments |
| `/attach <path>` / `/detach <id>` | add or remove a model or companion file |
| `/use <id>` | make a resident model active |
| `/recent` | show recent models |
| `/info` | show entity counts |
| `/save [path]` / `/reload` | keep or discard changes |

### Session and browser

| command | use |
| ------- | --- |
| `/mode [ask\|edit]` | show or change AI authority |
| `/sandbox [auto\|strict\|off\|restart]` | control generated-code isolation |
| `/viewer [off\|url]` | open, close, or print the viewer URL |
| `/chat [solo\|off\|provider]` | control browser chat |
| `/connect [client\|all]` | show and copy client setup |
| `/copy [client\|url\|viewer\|token]` | copy connection data |
| `/port <n>` | move the HTTP server |
| `/theme [light\|dark\|modern\|blue]` | change the shared console, viewer, and chat theme |

### Help and diagnostics

| command | use |
| ------- | --- |
| `/status` | show session status |
| `/tools [section]` | inspect commands, AI tools, prompts, resources, or settings |
| `/kb [query]` | search the offline IFC reference |
| `/settings [key value]` | inspect or change settings |
| `/audit [n]` | show recent audit records |
| `/help [command]` | show help |
| `/clear` / `/quit` | clear the feed or exit |

Unique prefixes work when unambiguous, so `/stat` runs `/status`.

## Files and workspaces

Start the console in your model folder. `/file` lists recents, supported IFC
files in that folder, and files one level below it. Type part of a name to
filter, or pass any allowed absolute or relative path.

Most sessions need one model. For coordination:

```text
> /workspace C:/models/project
> /attach structural.ifc
> /attach requirements.ids
> /models
```

Only the active model is writable. Attached IFC models are read-only; IDS, BCF,
and CSV files are companion paths. Workspace settings limit scan depth, model
count, and total memory. Dirty models are never evicted.

## Tool catalog

`/tools` reads the live registries, including enabled plugins and viewer tools.

```text
/tools ai query_elements
/tools settings sandbox.mode
/tools search validation
```

The catalog shows schemas and permissions but does not run tools or grant
authority.

## Edit, save, and exit

`ask` is read-only. `/mode edit` allows in-memory changes after confirmation.
Use `/save` to keep them, `/reload` to discard them, and `/mode ask` to lock the
model again. AI saving remains disabled unless separately enabled.

Select feed text and press ++ctrl+c++ to copy it. With no selection, ++ctrl+c++
exits. `/quit` and ++ctrl+q++ also exit and warn about unsaved changes.
