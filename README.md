# discord-rpc

Shows the `$>` typing-dots GIF (Homebrew green) as Chun's Discord Rich Presence
on Chun's Macs (silver MacBook, Mac mini). Python standard library only; uses Discord's local IPC
socket (no account token).

    bash install.sh              install + start at every login, then verify
    bash install.sh --status     running? last log lines
    bash install.sh --uninstall  stop and remove the login item

Timer: counts from first start and survives reboots. Restart the count with
`/opt/homebrew/bin/python3 ~/Library/Application\ Support/discord-rpc/presence.py --reset-timer`,
or set `DISCORD_RPC_TIMER=login` to restart it at each login.

Log: `~/Library/Logs/discord-rpc.log`

Tests (any OS, no Discord needed): `python3 -m unittest discover tests`
