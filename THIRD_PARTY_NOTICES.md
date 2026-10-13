# Third-party notices

Headroom's built-in Codex, Cursor, z.ai, and Antigravity collectors adapt provider protocol and credential-discovery behavior from [CodexBar](https://github.com/steipete/CodexBar), by Peter Steinberger and contributors.

The reviewed source revision is [`3a45dc7cf7c262fd26601fa0edfafddbf5ab465a`](https://github.com/steipete/CodexBar/tree/3a45dc7cf7c262fd26601fa0edfafddbf5ab465a). The relevant reference files are in `Sources/CodexBarCore/Providers/Codex/`, `Providers/Cursor/`, `Providers/Zai/`, the z.ai quota fetcher, and `Providers/Antigravity/`.

The collector logic is implemented within Headroom. Running Headroom does not require the CodexBar application, command-line executable, Swift package, or an upstream source checkout.

CodexBar is licensed under the MIT license. Its copyright and license notice are reproduced in [third_party/CodexBar-LICENSE.txt](third_party/CodexBar-LICENSE.txt). Headroom's own [MIT license](LICENSE) remains in effect.
