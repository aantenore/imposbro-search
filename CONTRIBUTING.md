# Contributing to IMPOSBRO Search

Thank you for your interest in contributing to IMPOSBRO Search! This document provides guidelines and information for contributors.

## 🚀 Getting Started

### Prerequisites

- Docker and Docker Compose
- Node.js 20.9+ (22+ recommended, matching the Docker image)
- Python 3.11+ (for backend development)
- Git

### Local Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/imposbro-search.git
   cd imposbro-search
   ```

2. **Set up environment**
   ```bash
   cp .env.example .env
   ```

3. **Start all services**
   ```bash
   docker-compose up --build
   ```

4. **Access the applications**
   - Admin UI: http://localhost:3001
   - API Docs: http://localhost:8000/docs
   - Grafana: http://localhost:3000

## 📁 Project Structure

```
imposbro-search/
├── query_api/          # FastAPI backend (Python)
├── admin_ui/           # Next.js frontend (React)
├── indexing_service/   # Kafka consumer (Python)
├── monitoring/         # Prometheus + Grafana configs
├── helm/               # Kubernetes deployment
└── docker-compose.yml  # Local development
```

## 🔧 Development Guidelines

### Code Style

**Python (Backend)**
- Follow PEP 8 style guidelines
- Use type hints for all function signatures
- Add docstrings to all public functions and classes
- Use `logging` instead of `print` statements

**JavaScript/React (Frontend)**
- Use functional components with hooks
- Add JSDoc comments to component props
- Follow the existing component library patterns in `components/ui/`

### Commit Messages

Use conventional commit format:

```
type(scope): description

[optional body]
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `refactor`: Code refactoring
- `test`: Adding tests
- `chore`: Maintenance tasks

Example:
```
feat(api): add batch ingestion endpoint

- Supports up to 1000 documents per request
- Includes validation for document schema
```

### Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with appropriate tests
3. Ensure all services build successfully
4. Update documentation if needed
5. Submit a PR with a clear description

## 🧪 Testing

### Running Tests

```bash
# From repo root (recommended)
make test          # Unix/macOS
npm run test       # Any OS (runs query_api pytest)
make audit         # npm + Python dependency audit

# From query_api/
cd query_api
pip install -r requirements-dev.txt
python -m pytest tests -v   # TESTING=1 is set in conftest

# From indexing_service/
cd indexing_service
pip install -r requirements.txt
python -m pytest tests -v

# Integration tests (require live Kafka/Redis/Typesense)
# By default they are skipped. To run them:
docker-compose up -d   # start the stack
cd query_api && INTEGRATION=1 python -m pytest tests -v -m integration
# Or exclude integration from CI: pytest tests -v -m "not integration"
```

The Admin UI proxies `/api/*` to the Query API via `app/api/[[...path]]/route.js`; set `INTERNAL_QUERY_API_URL` (e.g. in `.env`) to the backend URL. If `ADMIN_API_KEY` is enabled, set `INTERNAL_QUERY_API_ADMIN_API_KEY` or reuse `ADMIN_API_KEY` server-side so the proxy can authenticate admin requests without exposing the key to browser JavaScript.

### Manual Testing Checklist

- [ ] All Docker services start without errors
- [ ] Admin UI loads and navigation works
- [ ] Can register a new cluster
- [ ] Can create a collection
- [ ] Can set routing rules
- [ ] Document ingestion works
- [ ] Federated search returns results

## 📝 Documentation

- Update `README.md` for user-facing changes
- Add JSDoc/docstrings for new code
- Update API documentation in code comments
- Follow patterns in **[docs/PATTERNS_AND_PRACTICES.md](docs/PATTERNS_AND_PRACTICES.md)** (DI, validation, security, error handling)

## 🐛 Reporting Issues

When reporting a bug, please include:
- Steps to reproduce
- Expected behavior
- Actual behavior
- Docker logs if applicable
- Environment details

## 💡 Feature Requests

Feature requests are welcome! Please describe:
- The problem you're trying to solve
- Your proposed solution
- Any alternatives you've considered

## 📄 License

By contributing, you agree that your contributions will be licensed under the same license as the project.

---

Thank you for contributing to IMPOSBRO Search! 🎉
