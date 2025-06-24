# Welcome Plugin

This is a [dismob](https://github.com/dismob/dismob) plugin which will post a message when a new member join the discord server, and add a button to allow other members to greet the new member.

An event is available to allow other plugins to do some actions when a member greets another member (for example adding xp).

## Installation

> [!IMPORTANT]
> You need to have an already setup [dismob](https://github.com/dismob/dismob) bot. Follow the instruction there to do it first.

Just download/clone (or add as submodule) this repo into your dismob's `plugins` folder.  
The path **must** be `YourBot/plugins/welcome/main.py` at the end.

Once your bot is up and live, run those commands on your discord server:

```
!modules load welcome
!sync
```

> [!NOTE]
> Replace the prefix `!` by your own bot prefix when doing those commands!

Then you can reload your discord client with `Ctrl+R` to see the new slash commands.

## Commands

Command | Description
--- | ---
`/welcome [join\|leave] settings [<channel>] [<title>] [<enable>] [<duration>]` | Create or update the configuration for the join (or leave) messages on the server.
`/welcome [join\|leave] add-message <message>` | Add a new join (or leave) message to be chosen randomly
`/welcome [join\|leave] remove-message <id>` | Remove a message using its id (use `list-message` to get the id)
`/welcome [join\|leave] list-message` | Display the list of all join (or leave) messages
`/welcome [join\|leave] test` | Test the join (or leave) message
