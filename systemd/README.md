# systemd/ — daily arxiv alerts timer

User-level systemd timer that runs `scripts/arxiv_alerts_check.py` once per
day at 09:30 local time (with up to 10 min of randomized jitter). Logs to
`logs/arxiv_alerts.jsonl`.

## Why user-level (not system-level)
The arxiv-mcp-server stores watches in `~/.arxiv-mcp-server/papers/` — a
per-user state directory. Running the timer as the same user keeps watch
discovery, log paths, and HOME-relative installs aligned.

## Install (review first, then run yourself)
```bash
mkdir -p ~/.config/systemd/user
cp systemd/claude-arxiv-alerts.service ~/.config/systemd/user/
cp systemd/claude-arxiv-alerts.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-arxiv-alerts.timer
```

## Verify
```bash
systemctl --user list-timers claude-arxiv-alerts.timer
systemctl --user status claude-arxiv-alerts.service
journalctl --user -u claude-arxiv-alerts.service -n 50
tail -n 20 logs/arxiv_alerts.jsonl
```

## Run on demand (without waiting for the timer)
```bash
systemctl --user start claude-arxiv-alerts.service
# or directly:
python3 scripts/arxiv_alerts_check.py --dry-run --lookback-days 7
```

## Uninstall
```bash
systemctl --user disable --now claude-arxiv-alerts.timer
rm ~/.config/systemd/user/claude-arxiv-alerts.{service,timer}
systemctl --user daemon-reload
```

## Notes
- `Persistent=true` ensures missed firings (laptop asleep) catch up on next boot
- The service runs as `oneshot` and exits — no long-running daemon
- `ProtectSystem=strict` + `ReadWritePaths` confines writes to logs/ and the arxiv MCP papers dir
- Networking required: queries `export.arxiv.org` over HTTP. No auth, no rate-limit issues at 1 req/day per watch
- The script does NOT touch the MCP's `last_checked` field — manual `check_alerts` calls remain authoritative for in-session deltas
