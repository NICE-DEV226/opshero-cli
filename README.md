# 🚀 OpsHero CLI

Analyze CI/CD pipeline errors from your terminal in <100ms.

## Installation

```bash
pip install opshero
```

## Quick Start

```bash
# Login with GitHub
opshero login

# Analyze an error
opshero analyze "npm ERR! code ENOENT"

# Pipe from your CI/CD
npm test 2>&1 | opshero analyze

# View history
opshero history

# Search patterns
opshero patterns search docker
```

## Features

- ⚡ **Ultra-fast analysis** - <100ms for 85% of errors
- 🎯 **100+ patterns** - Docker, Node.js, Python, Git, K8s, tests
- 🤖 **AI fallback** - Groq LLM for unknown errors
- 📊 **History tracking** - All analyses saved and searchable
- 🔄 **Sync** - Access from CLI, dashboard, or API
- 🌐 **Offline mode** - Local pattern matching

## Commands

### Authentication

```bash
opshero login          # Login with GitHub
opshero logout         # Logout
opshero whoami         # Show current user
```

### Analysis

```bash
opshero analyze TEXT                    # Analyze error text
opshero analyze --file error.log        # Analyze from file
echo "error" | opshero analyze          # Pipe input
opshero analyze --language nodejs      # With context
```

### History

```bash
opshero history                # List all analyses
opshero history --last 10      # Last 10 analyses
opshero history show ID        # Show details
opshero history search docker  # Search in history
```

### Patterns

```bash
opshero patterns list                  # List all patterns
opshero patterns list --category docker # Filter by category
opshero patterns show ID               # Show pattern details
opshero patterns search "permission"   # Search patterns
```

### Contributions

```bash
opshero contribute pattern             # Submit new pattern
opshero contribute list                # My contributions
opshero contribute status ID           # Check status
```

### Sync

```bash
opshero sync analyses    # Sync analyses from cloud
opshero sync patterns    # Sync patterns
opshero sync all         # Sync everything
```

### Configuration

```bash
opshero config show                           # Show config
opshero config set api_url https://api...    # Set value
opshero config reset                          # Reset to defaults
```

## Examples

### Analyze npm Error

```bash
$ opshero analyze "npm ERR! code ENOENT"

✅ Analysis Complete (47ms)

Pattern Matched: npm_missing_file
Confidence: 95%

Problem:
  npm cannot find a required file or module

Solutions:
  1. npm install (confidence: 95%)
     Install missing dependencies
     
  2. Check package.json (confidence: 80%)
     Verify all dependencies are listed
     
  3. Clear cache (confidence: 60%)
     npm cache clean --force && npm install
```

### Pipe from CI/CD

```bash
# In your CI/CD script
npm test 2>&1 | opshero analyze

# Or with Docker
docker build . 2>&1 | opshero analyze
```

### Search History

```bash
$ opshero history search docker

Found 5 analyses:

1. docker_permission_denied (2 days ago)
   Status: Resolved
   
2. docker_build_failed (1 week ago)
   Status: Resolved
   
...
```

## Configuration

Config file: `~/.config/opshero/config.json`

```json
{
  "api_url": "https://api.opshero.dev",
  "cache_enabled": true,
  "offline_mode": false
}
```

## Environment Variables

```bash
OPSHERO_API_URL      # Override API URL
OPSHERO_TOKEN        # Auth token (set by login)
OPSHERO_OFFLINE      # Enable offline mode
```

## Development

```bash
# Clone repo
git clone https://github.com/your-org/opshero.git
cd opshero/cli

# Install in editable mode
pip install -e .

# Run tests
pytest

# Lint
ruff check .
```

## Links

- **Dashboard**: https://opsherodev.vercel.app
- **Admin**: https://opshero-admin.vercel.app
- **API**: https://opshero-backend-production.up.railway.app
- **GitHub Backend**: https://github.com/NICE-DEV226/opshero-backend
- **GitHub Dashboard**: https://github.com/NICE-DEV226/opshero-web
- **GitHub Admin**: https://github.com/NICE-DEV226/opshero-admin

## License

MIT License - see LICENSE file for details

## Support

- Email: opshero.dev@gmail.com
- GitHub Issues: https://github.com/NICE-DEV226/opshero-backend/issues
