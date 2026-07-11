module github.com/GALIAIS/CPA-GrokRegister/plugin/go

go 1.22

// Build against a local CLIProxyAPI checkout (recommended on the CPA host):
//
//   replace github.com/router-for-me/CLIProxyAPI/v7 => /path/to/CLIProxyAPI
//
// Or set GOPROXY and use a tagged module version of CLIProxyAPI if available.
//
// This plugin only needs sdk/pluginabi + sdk/pluginapi.

require github.com/router-for-me/CLIProxyAPI/v7 v7.0.0
