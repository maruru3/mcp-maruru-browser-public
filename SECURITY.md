# Security Policy

`mcp-maruru-browser` is a local-first MCP server that controls Chrome through a persistent user profile. It is designed to operate as the user, not as a sandboxed automation agent.

## Threat model

- The persistent Chrome profile contains active login state for every site the user has logged into.
- Any MCP tool call can interact with those authenticated browser sessions.
- `cookies_get` and `local_storage_get` can dump credentials, session tokens, and sensitive site data from logged-in contexts.
- `browser_evaluate`, `x_search`, `google_search`, `generic_form_fill`, and `chatgpt_ask` / `perplexity_search` can run JavaScript or interact with DOM in authenticated browser contexts.
- A compromised or malicious MCP client connected to this server effectively gains the user's web session for every logged-in site.

## Mitigation

- Run this MCP server only on your own machine, in a single-user local environment.
- Never expose this server to remote agents, public agent hosts, shared machines, or multi-tenant systems.
- Use a dedicated isolated Chrome profile for risky or untrusted workflows.
- Do not place `chrome-profile/` inside the repository, and never commit the profile directory.
- Audit MCP client configurations periodically — only allow trusted clients to connect.

## Reporting

This is a personal local-first project. If you discover a security issue while using or forking this code, open an issue or contact the maintainer directly.
