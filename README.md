# compress

`compress` reduces Codex tool output before it enters the model context. Everest
offers the service for free.

Your terminal retains the original output. If compression is unavailable or an
output cannot be compressed safely, Codex receives the original output.

## Install

`compress` requires macOS or Linux, Bash or Zsh, and the `codex` command.

```sh
curl -fsSL https://raw.githubusercontent.com/spenmcke/compress/refs/heads/main/scripts/install.sh | sh
```

The installer verifies the package checksum, installs the `compress` command,
adds the Codex shell integration, and starts browser login through Everest.
Open a new terminal after installation, then run `codex` normally.

Useful commands:

```sh
compress savings          # show estimated savings for the latest run
compress doctor           # check the service connection
compress update --check   # check for an update
codex --uncompressed      # run Codex without compression
compress uninstall        # remove the integration and CLI
```

Uninstalling preserves local credentials, configuration, and savings history.
Use `compress uninstall --purge` to remove those as well.

## Privacy

Everest does not log your coding sessions. Eligible tool output, the associated
tool arguments, a focus description, and up to 4,000 characters of the latest
user message are sent to Everest for compression. The service processes that
content to return the compressed result; it does not store the session content.

The service records operational metadata and aggregate usage, such as request
outcomes, timing, and token counts. Local event logs contain metadata such as
hashes, counts, outcomes, and timing. Codex's own local session files can contain
the original content.

## Updates

`compress` checks for updates at most once every 24 hours when a Codex run
starts. Set `COMPRESS_AUTO_UPDATE=0` to disable automatic updates.

Savings are estimates and do not guarantee lower bills or identical model
behavior.

## Source

This repository contains the Codex CLI, installer, and published release
artifacts. The hosted compression service, model weights, customer SDKs, and
deployment configuration are not included.

## License

MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
