# compress

`everest` reduces Codex tool output before it enters the model context. Everest
offers the service for free.

Your terminal retains the original output. If compression is unavailable or an
output cannot be compressed safely, Codex receives the original output.

## Install

`everest` requires macOS or Linux, Bash or Zsh, and please ensure codex is installed beforehand.

```
curl -fsSL https://install.everestagi.com/install.sh | sh && source ~/.config/everest/shell.sh
```

The installer verifies the package checksum, installs the `compress` command,
adds the Codex shell integration, and starts browser login through Everest.
Open a new terminal after installation, then run `codex` normally.

Useful commands:

```sh
everest savings          # show estimated savings for the latest run
everest doctor           # check the service connection
everest update --check   # check for an update
codex --uncompressed      # run Codex without compression
everest uninstall        # remove the integration and CLI
```

Uninstalling preserves local credentials, configuration, and savings history.
Use `everest uninstall --purge` to remove those as well.

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
